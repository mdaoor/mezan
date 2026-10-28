"""Ingest (transcode + peaks + preview), segmentation and dataset-import jobs."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from sqlalchemy import delete, func, select

from mizan.core import models as m
from mizan.core.audio import compute_peaks, make_preview_opus, parse_wav, read_pcm, transcode_to_wav16k
from mizan.core.blobstore import audio_prefix
from mizan.core.jobs import enqueue, handler
from mizan.plugins import registry as plugins
from mizan.plugins.textutil import duration_bucket

log = logging.getLogger("mizan.ingest")


async def _prefix(db, af: m.AudioFile) -> str:
    ds = await db.get(m.Dataset, af.dataset_id)
    return audio_prefix(ds.project_id, ds.id, af.id)


@handler("ingest")
async def ingest(c, job, ctx) -> dict:
    ids: list[str] = job.payload["audio_file_ids"]
    ready, failed = 0, []
    for n, afid in enumerate(ids):
        async with c.session() as db:
            af = await db.get(m.AudioFile, afid)
            if af is None:
                continue
            af.status = "processing"
            prefix = await _prefix(db, af)
            af_meta = dict(af.meta)
            src = c.blobs.local_path(af.original_blob)
            await db.commit()
        try:
            tmp = c.blobs.local_path(f"{prefix}/.normalized.{uuid.uuid4().hex}.wav")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            if af_meta.get("prenormalized"):  # imported archives already hold 16 kHz mono PCM16: keep bytes identical
                tmp.write_bytes(src.read_bytes())
            else:
                await transcode_to_wav16k(src, tmp)
            info = await asyncio.to_thread(parse_wav, tmp)
            c.blobs.put_file(f"{prefix}/normalized.wav", tmp)
            norm = c.blobs.local_path(f"{prefix}/normalized.wav")
            peaks = await asyncio.to_thread(compute_peaks, norm, info)
            c.blobs.put_bytes(f"{prefix}/peaks.json", json.dumps(peaks).encode())
            has_preview = await make_preview_opus(norm, c.blobs.local_path(f"{prefix}/preview.opus"))
            async with c.session() as db:
                af = await db.get(m.AudioFile, afid)
                af.normalized_blob = f"{prefix}/normalized.wav"
                af.duration_s, af.sample_rate, af.channels = info.duration_s, info.sample_rate, info.channels
                af.meta = {**af.meta, "preview": has_preview, "peaks_blob": f"{prefix}/peaks.json"}
                af.status = "ready"
                await db.commit()
            ready += 1
        except Exception as e:
            log.exception("ingest failed for %s", afid)
            failed.append({"audio_file_id": afid, "error": str(e)[:300]})
            async with c.session() as db:
                af = await db.get(m.AudioFile, afid)
                af.status, af.meta = "failed", {**af.meta, "error": str(e)[:500]}
                await db.commit()
        await ctx.progress(n + 1, len(ids), force=True)
    if job.payload.get("segmenter") and ready:
        async with c.session() as db:
            ds_id = (await db.get(m.AudioFile, ids[0])).dataset_id
            j = await enqueue(c, db, "segment", {"dataset_id": ds_id, "audio_file_ids": ids,
                                                 "segmenter": job.payload["segmenter"]}, job.created_by)
            await db.commit()
        c.worker and c.worker.wake()
        return {"ready": ready, "failed": failed, "segment_job_id": j.id}
    return {"ready": ready, "failed": failed}


async def segments_blocker(db, audio_file_id: str) -> str | None:
    """Re-segmenting deletes segments (and cascades). Allowed only when nothing durable hangs off them."""
    seg_ids = select(m.Segment.id).where(m.Segment.audio_file_id == audio_file_id)
    if (await db.execute(select(func.count()).select_from(m.GoldItem).where(m.GoldItem.segment_id.in_(seg_ids)))).scalar():
        return "segments are part of a frozen gold set"
    q = (select(func.count()).select_from(m.Hypothesis).join(m.Run, m.Run.id == m.Hypothesis.run_id)
         .where(m.Hypothesis.segment_id.in_(seg_ids), m.Run.type != "bootstrap"))
    if (await db.execute(q)).scalar():
        return "segments have evaluation results"
    q = select(func.count()).select_from(m.Annotation).where(
        m.Annotation.segment_id.in_(seg_ids), (m.Annotation.author_id.is_not(None)) | (m.Annotation.status != "draft"))
    if (await db.execute(q)).scalar():
        return "segments have human annotations"
    return None


@handler("segment")
async def segment(c, job, ctx) -> dict:
    p = job.payload
    cfg = p.get("segmenter") or {"kind": "energy_vad", "config": {}}
    seg = plugins.segmenters.get(cfg["kind"])()
    created, skipped = 0, []
    async with c.session() as db:
        q = select(m.AudioFile.id).where(m.AudioFile.dataset_id == p["dataset_id"], m.AudioFile.status == "ready")
        if p.get("audio_file_ids"):
            q = q.where(m.AudioFile.id.in_(p["audio_file_ids"]))
        ids = [r[0] for r in (await db.execute(q.order_by(m.AudioFile.created_at))).all()]
    for n, afid in enumerate(ids):
        async with c.session() as db:
            af = await db.get(m.AudioFile, afid)
            reason = await segments_blocker(db, afid)
            if reason:
                skipped.append({"audio_file_id": afid, "reason": reason})
                continue
            path = c.blobs.local_path(af.normalized_blob)
            info = await asyncio.to_thread(parse_wav, path)
            pcm = await asyncio.to_thread(read_pcm, path, info, 0, info.duration_s)
            spans = await asyncio.to_thread(seg.segment, pcm, info.duration_s, cfg.get("config", {}))
            await db.execute(delete(m.Segment).where(m.Segment.audio_file_id == afid))
            flags = [[] for _ in spans]
            for i in range(1, len(spans)):
                if spans[i][0] < spans[i - 1][1] - 1e-6:  # forced split inside speech
                    flags[i - 1], flags[i] = ["truncated"], ["truncated"]
            for i, (s, e) in enumerate(spans):
                db.add(m.Segment(audio_file_id=afid, idx=i, start_s=s, end_s=e, segmenter_kind=seg.kind,
                                 segmenter_version=seg.version, flags=flags[i],
                                 strata={"duration_bucket": duration_bucket(e - s)}))
            af.meta = {**af.meta, "segmenter": {"kind": seg.kind, "version": seg.version, "config": cfg.get("config", {})}}
            await db.commit()
            created += len(spans)
        await ctx.progress(n + 1, len(ids), force=True)
    return {"segments": created, "skipped": skipped}


@handler("import")
async def import_(c, job, ctx) -> dict:
    from mizan.core.services import portability

    async with c.session() as db:
        out = await portability.import_zip(c, db, job.payload["dataset_id"], Path(job.payload["zip_path"]), job.created_by)
        await db.commit()
    Path(job.payload["zip_path"]).unlink(missing_ok=True)
    if out.get("audio_file_ids"):
        async with c.session() as db:
            await enqueue(c, db, "ingest", {"audio_file_ids": out["audio_file_ids"], "imported": True}, job.created_by)
            await db.commit()
    return out
