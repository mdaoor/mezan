"""Dataset / gold-set import & export (F18). Deterministic zip so export -> import -> export is byte-identical."""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.audio import sha256_file
from mizan.core.blobstore import audio_prefix
from mizan.core.errors import Invalid, NotFound
from mizan.core.services.common import must_get
from mizan.core.services import gold as goldsvc
from mizan.plugins.textutil import lang_mix

FORMAT = "mizan-dataset/1"
_EPOCH = (1980, 1, 1, 0, 0, 0)


def _zi(name: str, compress: bool) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, _EPOCH)
    zi.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zi.external_attr = 0o644 << 16
    return zi


def _jline(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def export_zip(c, db: AsyncSession, dataset_id: str, gold_selector: str | None, out_path: Path) -> dict:
    await must_get(db, m.Dataset, dataset_id, "Dataset")
    gs = await goldsvc.get_by_selector(db, dataset_id, gold_selector) if gold_selector else None
    files = {af.id: af for af in (await db.execute(select(m.AudioFile).where(
        m.AudioFile.dataset_id == dataset_id, m.AudioFile.status == "ready"))).scalars()}
    q = select(m.Segment).where(m.Segment.audio_file_id.in_(files)).order_by(m.Segment.audio_file_id, m.Segment.idx)
    segs = (await db.execute(q)).scalars().all()

    texts: dict[str, dict] = {}
    if gs:
        for it in (await db.execute(select(m.GoldItem).where(m.GoldItem.gold_set_id == gs.id))).scalars():
            texts[it.segment_id] = {"text": it.text, "spans": it.spans, "strata": it.strata, "flags": it.flags}
    else:
        for s in segs:
            a = (await db.execute(select(m.Annotation).where(
                m.Annotation.segment_id == s.id, m.Annotation.track == "primary", m.Annotation.status == "approved")
                .order_by(m.Annotation.version.desc()).limit(1))).scalar_one_or_none()
            if a:
                texts[s.id] = {"text": a.text, "spans": a.spans, "strata": s.strata, "flags": s.flags}
    sha_of: dict[str, str] = {}
    for afid, af in files.items():
        sha_of[afid] = sha256_file(c.blobs.local_path(af.normalized_blob))

    lines = []
    for s in segs:
        if gs and s.id not in texts:
            continue
        t = texts.get(s.id)
        rec = {"audio": f"audio/{sha_of[s.audio_file_id]}.wav", "audio_sha256": sha_of[s.audio_file_id], "index": s.idx,
               "start_s": s.start_s, "end_s": s.end_s, "flags": t["flags"] if t else s.flags,
               "strata": t["strata"] if t else s.strata}
        if t:
            rec.update(text=t["text"], spans=t["spans"])
        lines.append(rec)
    lines.sort(key=lambda r: (r["audio_sha256"], r["index"]))
    manifest = {"format": FORMAT, "gold_set": {"name": gs.name, "version": gs.version, "policy": gs.policy} if gs else None,
                "audio_files": len({r["audio_sha256"] for r in lines}), "segments": len(lines)}
    used = sorted({r["audio_sha256"] for r in lines})
    path_of = {sha_of[a]: c.blobs.local_path(files[a].normalized_blob) for a in files}
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr(_zi("manifest.json", True), json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
        z.writestr(_zi("segments.jsonl", True), "".join(_jline(r) + "\n" for r in lines))
        for sha in used:
            with z.open(_zi(f"audio/{sha}.wav", False), "w", force_zip64=True) as dst, open(path_of[sha], "rb") as src:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
    return manifest


async def import_zip(c, db: AsyncSession, dataset_id: str, zip_path: Path, user_id: str | None) -> dict:
    ds = await must_get(db, m.Dataset, dataset_id, "Dataset")
    try:
        z = zipfile.ZipFile(zip_path)
        manifest = json.loads(z.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise Invalid(f"not a Mizan dataset archive: {e}") from e
    if manifest.get("format") != FORMAT:
        raise Invalid(f"unsupported archive format '{manifest.get('format')}'")
    recs = [json.loads(line) for line in z.read("segments.jsonl").decode("utf-8").splitlines() if line.strip()]

    existing = {af.sha256: af for af in (await db.execute(select(m.AudioFile).where(m.AudioFile.dataset_id == dataset_id))).scalars()}
    new_ids: list[str] = []
    for sha in sorted({r["audio_sha256"] for r in recs}):
        if sha in existing:
            continue
        af = m.AudioFile(dataset_id=dataset_id, original_blob="", sha256=sha, meta={"original_name": f"{sha[:12]}.wav", "prenormalized": True})
        db.add(af)
        await db.flush()
        key = f"{audio_prefix(ds.project_id, ds.id, af.id)}/original.wav"
        tmp = Path(tempfile.mkstemp(dir=c.settings.data_dir, suffix=".wav")[1])
        with z.open(f"audio/{sha}.wav") as src, open(tmp, "wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
        c.blobs.put_file(key, tmp)
        af.original_blob = key
        existing[sha] = af
        new_ids.append(af.id)

    seg_by_key: dict[tuple, m.Segment] = {}
    made, annotated = 0, 0
    items: list[tuple[m.Segment, dict]] = []
    for r in recs:
        af = existing[r["audio_sha256"]]
        key = (af.id, round(r["start_s"], 3), round(r["end_s"], 3))
        if key not in seg_by_key:
            for s in (await db.execute(select(m.Segment).where(m.Segment.audio_file_id == af.id))).scalars():
                seg_by_key[(af.id, round(s.start_s, 3), round(s.end_s, 3))] = s
        seg = seg_by_key.get(key)
        if seg is None:
            idx = r["index"]
            used = set((await db.execute(select(m.Segment.idx).where(m.Segment.audio_file_id == af.id))).scalars())
            while idx in used:
                idx += 1_000_000
            seg = m.Segment(audio_file_id=af.id, idx=idx, start_s=r["start_s"], end_s=r["end_s"], segmenter_kind="manual_import",
                            segmenter_version="1.0.0", strata=r.get("strata", {}), flags=r.get("flags", []))
            db.add(seg)
            await db.flush()
            seg_by_key[key] = seg
            made += 1
        if "text" in r:
            db.add(m.Annotation(segment_id=seg.id, version=1 + len((await db.execute(select(m.Annotation.id).where(
                m.Annotation.segment_id == seg.id))).all()), text=r["text"], spans=r.get("spans", []), status="approved",
                source="import"))
            annotated += 1
            items.append((seg, r))
    out = {"audio_file_ids": new_ids, "segments_created": made, "annotations": annotated, "gold_set_id": None}
    gmeta = manifest.get("gold_set")
    if gmeta and items:
        taken = set((await db.execute(select(m.GoldSet.version).where(m.GoldSet.dataset_id == dataset_id))).scalars())
        ver = gmeta["version"]
        while ver in taken:
            ver += 1
        gs = m.GoldSet(dataset_id=dataset_id, version=ver, name=gmeta["name"], policy=gmeta["policy"])
        db.add(gs)
        await db.flush()
        for seg, r in items:
            a = (await db.execute(select(m.Annotation).where(m.Annotation.segment_id == seg.id).order_by(m.Annotation.version.desc()).limit(1))).scalar_one()
            db.add(m.GoldItem(gold_set_id=gs.id, segment_id=seg.id, annotation_id=a.id, text=r["text"], spans=r.get("spans", []),
                              strata=r.get("strata", {}), flags=r.get("flags", [])))
        out.update(gold_set_id=gs.id, gold_version=ver)
    return out
