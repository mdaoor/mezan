import asyncio
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api import schemas as S
from mizan.api.deps import annotator, container, current_user, get_db, reviewer
from mizan.core import models as m
from mizan.core.audio import cut_wav, parse_wav
from mizan.core.errors import Invalid, NotFound
from mizan.core.jobs import enqueue, wake
from mizan.core.services import annotation as annsvc
from mizan.core.services import datasets as dssvc
from mizan.core.services import gold as goldsvc
from mizan.core.services import portability, runs as runsvc
from mizan.core.services.common import Actor, must_get, page_params
from mizan.plugins import registry as plugins

router = APIRouter(tags=["datasets"])


# ---------------------------------------------------------------- projects
@router.post("/projects", status_code=201)
async def create_project(body: S.ProjectIn, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    p = await dssvc.create_project(db, actor, body.name, body.description, body.settings)
    await db.commit()
    return {"id": p.id, "name": p.name, "description": p.description, "settings": p.settings, "created_at": p.created_at}


@router.get("/projects")
async def list_projects(_: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(m.Project).order_by(m.Project.created_at.desc()))).scalars()
    return {"items": [{"id": p.id, "name": p.name, "description": p.description, "created_at": p.created_at} for p in rows]}


@router.get("/projects/{project_id}")
async def get_project(project_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    p = await must_get(db, m.Project, project_id, "Project")
    ds = (await db.execute(select(m.Dataset).where(m.Dataset.project_id == p.id).order_by(m.Dataset.created_at))).scalars().all()
    return {"id": p.id, "name": p.name, "description": p.description, "settings": p.settings, "created_at": p.created_at,
            "datasets": [{"id": d.id, "name": d.name} for d in ds]}


@router.post("/projects/{project_id}/datasets", status_code=201)
async def create_dataset(project_id: str, body: S.DatasetIn, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    d = await dssvc.create_dataset(db, actor, project_id, body.name, body.description, body.language_hints)
    await db.commit()
    return {"id": d.id, "project_id": d.project_id, "name": d.name, "description": d.description, "language_hints": d.language_hints}


@router.get("/projects/{project_id}/datasets")
async def list_datasets(project_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(m.Dataset).where(m.Dataset.project_id == project_id).order_by(m.Dataset.created_at))).scalars().all()
    out = []
    for d in rows:
        st = await dssvc.dataset_stats(db, d.id)
        out.append({"id": d.id, "name": d.name, "description": d.description, "language_hints": d.language_hints,
                    "stats": {k: st[k] for k in ("files", "segments", "audio_minutes", "progress", "gold_sets")}})
    return {"items": out}


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = await must_get(db, m.Dataset, dataset_id, "Dataset")
    gs = (await db.execute(select(m.GoldSet).where(m.GoldSet.dataset_id == d.id).order_by(m.GoldSet.version))).scalars().all()
    return {"id": d.id, "project_id": d.project_id, "name": d.name, "description": d.description,
            "language_hints": d.language_hints, "stats": await dssvc.dataset_stats(db, d.id),
            "gold_sets": [{"id": g.id, "version": g.version, "name": g.name, "frozen_at": g.frozen_at} for g in gs]}


# ------------------------------------------------------------------- audio
@router.post("/datasets/{dataset_id}/audio", status_code=202)
async def upload_audio(dataset_id: str, files: list[UploadFile] = File(...), segmenter: str | None = Query(None, description="auto-segment after ingest with this segmenter kind"),
                       actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    ds = await must_get(db, m.Dataset, dataset_id, "Dataset")
    new_ids, accepted = [], []
    for f in files:
        af, is_new = await dssvc.add_audio_file(c, db, ds, f.filename or "audio", f)
        accepted.append({"audio_file_id": af.id, "filename": f.filename, "duplicate": not is_new})
        if is_new:
            new_ids.append(af.id)
    payload = {"audio_file_ids": new_ids}
    if segmenter:
        if not plugins.segmenters.has(segmenter):
            raise Invalid(f"unknown segmenter '{segmenter}'")
        payload["segmenter"] = {"kind": segmenter, "config": {}}
    job = await enqueue(c, db, "ingest", payload, actor.id) if new_ids else None
    await db.commit()
    wake(c)
    return {"job_id": job.id if job else None, "files": accepted}


@router.get("/datasets/{dataset_id}/audio")
async def list_audio(dataset_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(m.AudioFile).where(m.AudioFile.dataset_id == dataset_id).order_by(m.AudioFile.created_at))).scalars()
    out = []
    for a in rows:
        n = (await db.execute(select(func.count()).select_from(m.Segment).where(m.Segment.audio_file_id == a.id))).scalar()
        out.append({"id": a.id, "name": a.meta.get("original_name"), "status": a.status, "duration_s": a.duration_s,
                    "sample_rate": a.sample_rate, "channels": a.channels, "segments": n, "error": a.meta.get("error"),
                    "segmenter": a.meta.get("segmenter")})
    return {"items": out}


@router.get("/audio/{audio_id}/stream")
async def stream_audio(audio_id: str, start: float | None = None, end: float | None = None, _: Actor = Depends(current_user),
                       c=Depends(container), db: AsyncSession = Depends(get_db)):
    af = await must_get(db, m.AudioFile, audio_id, "AudioFile")
    if not af.normalized_blob:
        raise NotFound("audio not ready")
    path = c.blobs.local_path(af.normalized_blob)
    if start is None and end is None:
        return FileResponse(path, media_type="audio/wav")  # Range requests handled by Starlette
    info = await asyncio.to_thread(parse_wav, path)
    s, e = max(0.0, start or 0.0), min(info.duration_s, end if end is not None else info.duration_s)
    if e <= s:
        raise Invalid("empty range")
    data = await asyncio.to_thread(cut_wav, path, info, s, e)
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/audio/{audio_id}/peaks")
async def audio_peaks(audio_id: str, _: Actor = Depends(current_user), c=Depends(container), db: AsyncSession = Depends(get_db)):
    af = await must_get(db, m.AudioFile, audio_id, "AudioFile")
    key = af.meta.get("peaks_blob")
    if not key or not c.blobs.exists(key):
        raise NotFound("peaks not available")
    return Response(c.blobs.read(key), media_type="application/json", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/audio/{audio_id}/preview")
async def audio_preview(audio_id: str, _: Actor = Depends(current_user), c=Depends(container), db: AsyncSession = Depends(get_db)):
    af = await must_get(db, m.AudioFile, audio_id, "AudioFile")
    if not af.normalized_blob:
        raise NotFound("audio not ready")
    prev = c.blobs.local_path(af.normalized_blob).with_name("preview.opus")
    if prev.exists():
        return FileResponse(prev, media_type="audio/ogg")
    return FileResponse(c.blobs.local_path(af.normalized_blob), media_type="audio/wav")


# ---------------------------------------------------------------- segments
@router.post("/datasets/{dataset_id}/segment", status_code=202)
async def segment_dataset(dataset_id: str, body: S.SegmentIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    await must_get(db, m.Dataset, dataset_id, "Dataset")
    if not plugins.segmenters.has(body.segmenter.kind):
        raise Invalid(f"unknown segmenter '{body.segmenter.kind}'", available=plugins.segmenters.keys())
    job = await enqueue(c, db, "segment", {"dataset_id": dataset_id, "segmenter": body.segmenter.model_dump(),
                                           "audio_file_ids": body.audio_file_ids}, actor.id)
    await db.commit()
    wake(c)
    return {"job_id": job.id}


@router.post("/datasets/{dataset_id}/bootstrap", status_code=202)
async def bootstrap(dataset_id: str, body: S.BootstrapIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    run = await runsvc.create_run(c, db, actor, {"type": "bootstrap", "name": body.name, "model_endpoint_id": body.model_endpoint_id,
                                                 "dataset_id": dataset_id, "options": body.options, "reference": {"type": "none"}})
    await db.commit()
    wake(c)
    return {"job_id": run.id, "run_id": run.id}


@router.get("/datasets/{dataset_id}/segments")
async def list_segments(dataset_id: str, status: str | None = None, claimed_by: str | None = None, stratum: str | None = None,
                        flag: str | None = None, audio_file_id: str | None = None, limit: int | None = None, cursor: str | None = None,
                        actor: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    lim, off = page_params(limit, cursor, 100, 1000)
    latest_v = select(m.Annotation.segment_id, func.max(m.Annotation.version).label("v")).where(
        m.Annotation.track == "primary").group_by(m.Annotation.segment_id).subquery()
    q = (select(m.Segment, m.Annotation, m.AudioFile).join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .outerjoin(latest_v, latest_v.c.segment_id == m.Segment.id)
         .outerjoin(m.Annotation, (m.Annotation.segment_id == m.Segment.id) & (m.Annotation.version == latest_v.c.v) & (m.Annotation.track == "primary"))
         .where(m.AudioFile.dataset_id == dataset_id).order_by(m.AudioFile.created_at, m.Segment.audio_file_id, m.Segment.idx))
    if audio_file_id:
        q = q.where(m.Segment.audio_file_id == audio_file_id)
    if status == "unannotated":
        q = q.where(m.Annotation.id.is_(None))
    elif status:
        q = q.where(m.Annotation.status == status)
    if claimed_by:
        q = q.where(m.Annotation.claimed_by == (actor.id if claimed_by == "me" else claimed_by))
    rows = (await db.execute(q)).all()
    if stratum:
        k, _, v = stratum.partition("=")
        rows = [r for r in rows if str(r[0].strata.get(k)) == v]
    if flag:
        rows = [r for r in rows if flag in r[0].flags]
    page = rows[off: off + lim + 1]
    return {"items": [dssvc.segment_view(s, a, af) for s, a, af in page[:lim]], "next_cursor": str(off + lim) if len(page) > lim else None,
            "total": len(rows)}


@router.patch("/segments/{segment_id}")
async def patch_segment(segment_id: str, body: S.SegmentPatch, actor: Actor = annotator, db: AsyncSession = Depends(get_db)):
    s = await dssvc.patch_segment(db, actor, segment_id, body.strata, body.flags)
    await db.commit()
    return dssvc.segment_view(s)


@router.post("/segments/{segment_id}/queue")
async def queue_segment(segment_id: str, body: S.QueueIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await annsvc.queue_segment(c, db, actor, segment_id, body.priority)


# ---------------------------------------------------------------- gold sets
@router.post("/datasets/{dataset_id}/gold-sets", status_code=201)
async def freeze_gold(dataset_id: str, body: S.GoldSetIn, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    gs = await goldsvc.freeze(db, actor, dataset_id, body.name, body.policy)
    await db.commit()
    return await goldsvc.stats(db, gs)


@router.get("/datasets/{dataset_id}/gold-sets")
async def list_gold(dataset_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(m.GoldSet).where(m.GoldSet.dataset_id == dataset_id).order_by(m.GoldSet.version))).scalars().all()
    return {"items": [await goldsvc.stats(db, g) for g in rows]}


@router.get("/gold-sets/{gold_id}")
async def get_gold(gold_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await goldsvc.stats(db, await must_get(db, m.GoldSet, gold_id, "GoldSet"))


@router.get("/gold-sets/{gold_id}/items")
async def gold_items(gold_id: str, limit: int | None = None, cursor: str | None = None, _: Actor = Depends(current_user),
                     db: AsyncSession = Depends(get_db)):
    lim, off = page_params(limit, cursor, 100, 1000)
    q = (select(m.GoldItem, m.Segment).join(m.Segment, m.Segment.id == m.GoldItem.segment_id).where(m.GoldItem.gold_set_id == gold_id)
         .order_by(m.Segment.audio_file_id, m.Segment.idx).offset(off).limit(lim + 1))
    rows = (await db.execute(q)).all()
    return {"items": [{"segment_id": i.segment_id, "audio_file_id": s.audio_file_id, "start_s": s.start_s, "end_s": s.end_s, "text": i.text,
                       "spans": i.spans, "strata": i.strata, "flags": i.flags} for i, s in rows[:lim]],
            "next_cursor": str(off + lim) if len(rows) > lim else None}


@router.post("/datasets/{dataset_id}/iaa/sample")
async def iaa_sample(dataset_id: str, body: S.IAASampleIn, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    out = await goldsvc.mark_iaa_sample(db, actor, dataset_id, body.pct, body.seed)
    await db.commit()
    return out


@router.get("/datasets/{dataset_id}/iaa")
async def iaa(dataset_id: str, gold_set: str | None = None, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    gs = await goldsvc.get_by_selector(db, dataset_id, gold_set) if gold_set else None
    return await goldsvc.compute_iaa(db, dataset_id, gs)


# ------------------------------------------------------- import / export
@router.post("/datasets/{dataset_id}/import", status_code=202)
async def import_dataset(dataset_id: str, file: UploadFile = File(...), actor: Actor = reviewer, c=Depends(container),
                         db: AsyncSession = Depends(get_db)):
    await must_get(db, m.Dataset, dataset_id, "Dataset")
    tmp = c.settings.data_dir / "tmp" / f"import-{dataset_id}-{Path(file.filename or 'x').name}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    job = await enqueue(c, db, "import", {"dataset_id": dataset_id, "zip_path": str(tmp)}, actor.id)
    await db.commit()
    wake(c)
    return {"job_id": job.id}


@router.get("/datasets/{dataset_id}/export")
async def export_dataset(dataset_id: str, bg: BackgroundTasks, gold_set: str | None = None, _: Actor = Depends(current_user),
                         c=Depends(container), db: AsyncSession = Depends(get_db)):
    out = Path(tempfile.mkstemp(dir=c.settings.data_dir, suffix=".zip")[1])
    manifest = await portability.export_zip(c, db, dataset_id, gold_set, out)
    bg.add_task(out.unlink, missing_ok=True)
    name = f"dataset-{dataset_id[:8]}" + (f"-gold-{gold_set}" if gold_set else "") + ".zip"
    return FileResponse(out, media_type="application/zip", filename=name, headers={"X-Mizan-Segments": str(manifest["segments"])})
