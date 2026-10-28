from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.blobstore import audio_prefix
from mizan.core.errors import Conflict, Invalid
from mizan.core.services.common import Actor, audit, must_get

ALLOWED_EXT = {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".flac", ".opus", ".webm", ".aac", ".mkv"}
STANDARD_FLAGS = ("no_speech", "music", "overlap", "unintelligible", "truncated")
AUTO_STRATA = {"duration_bucket", "lang_mix", "snr_bucket"}


async def create_project(db: AsyncSession, actor: Actor, name: str, description: str = "", settings: dict | None = None):
    p = m.Project(name=name, description=description, settings=settings or {}, created_by=actor.id)
    db.add(p)
    await db.flush()
    audit(db, actor, "project.create", "project", p.id, {"name": name})
    return p


async def create_dataset(db: AsyncSession, actor: Actor, project_id: str, name: str, description: str = "",
                         language_hints: list[str] | None = None):
    await must_get(db, m.Project, project_id, "Project")
    d = m.Dataset(project_id=project_id, name=name, description=description,
                  language_hints=language_hints or [], created_by=actor.id)
    db.add(d)
    await db.flush()
    audit(db, actor, "dataset.create", "dataset", d.id, {"name": name})
    return d


async def add_audio_file(c, db: AsyncSession, dataset: m.Dataset, filename: str, stream) -> tuple[m.AudioFile | None, bool]:
    """Spool an upload to disk (hashing as we go). Returns (audio_file, is_new)."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise Invalid(f"unsupported audio type '{ext}'", allowed=sorted(ALLOWED_EXT))
    tmp = c.settings.data_dir / "tmp" / uuid.uuid4().hex
    tmp.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with open(tmp, "wb") as f:
        while chunk := await stream.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    sha = h.hexdigest()
    dup = (await db.execute(select(m.AudioFile).where(m.AudioFile.dataset_id == dataset.id,
                                                      m.AudioFile.sha256 == sha))).scalar_one_or_none()
    if dup:
        tmp.unlink(missing_ok=True)
        return dup, False
    af = m.AudioFile(dataset_id=dataset.id, original_blob="", sha256=sha, meta={"original_name": filename})
    db.add(af)
    await db.flush()
    key = f"{audio_prefix(dataset.project_id, dataset.id, af.id)}/original{ext}"
    c.blobs.put_file(key, tmp)
    af.original_blob = key
    return af, True


def segment_view(s: m.Segment, a: m.Annotation | None = None, af: m.AudioFile | None = None) -> dict:
    d = {"id": s.id, "audio_file_id": s.audio_file_id, "index": s.idx, "start_s": s.start_s, "end_s": s.end_s,
         "duration_s": s.end_s - s.start_s, "strata": s.strata, "flags": s.flags}
    if a is not None:
        d["annotation"] = {"id": a.id, "version": a.version, "status": a.status, "source": a.source,
                           "author_id": a.author_id, "claimed_by": a.claimed_by,
                           "claim_expires_at": a.claim_expires_at, "text": a.text}
    if af is not None:
        d["audio_name"] = af.meta.get("original_name")
    return d


async def patch_segment(db: AsyncSession, actor: Actor, seg_id: str, strata: dict | None, flags: list | None) -> m.Segment:
    s = await must_get(db, m.Segment, seg_id, "Segment")
    if strata is not None:
        s.strata = {**s.strata, **{k: v for k, v in strata.items() if k not in AUTO_STRATA or k == "snr_bucket"}}
    if flags is not None:
        bad = set(flags) - set(STANDARD_FLAGS)
        if bad:
            raise Invalid(f"unknown flags {sorted(bad)}", allowed=STANDARD_FLAGS)
        s.flags = sorted(set(flags))
    audit(db, actor, "segment.patch", "segment", s.id, {"strata": strata, "flags": flags})
    return s


async def dataset_stats(db: AsyncSession, dataset_id: str) -> dict:
    files = (await db.execute(select(m.AudioFile).where(m.AudioFile.dataset_id == dataset_id))).scalars().all()
    latest_v = select(m.Annotation.segment_id, func.max(m.Annotation.version).label("v")).where(
        m.Annotation.track == "primary").group_by(m.Annotation.segment_id).subquery()
    q = (select(m.Segment, m.Annotation.status).join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .outerjoin(latest_v, latest_v.c.segment_id == m.Segment.id)
         .outerjoin(m.Annotation, (m.Annotation.segment_id == m.Segment.id) & (m.Annotation.version == latest_v.c.v)
                    & (m.Annotation.track == "primary"))
         .where(m.AudioFile.dataset_id == dataset_id))
    rows = (await db.execute(q)).all()
    by_status: dict[str, dict] = {}
    for seg, status in rows:
        b = by_status.setdefault(status or "unannotated", {"segments": 0, "minutes": 0.0})
        b["segments"] += 1
        b["minutes"] += (seg.end_s - seg.start_s) / 60
    total_min = sum(b["minutes"] for b in by_status.values())
    done_min = by_status.get("approved", {}).get("minutes", 0.0)
    return {
        "files": len(files), "files_ready": sum(f.status == "ready" for f in files),
        "files_failed": sum(f.status == "failed" for f in files),
        "audio_minutes": sum((f.duration_s or 0) for f in files) / 60,
        "segments": len(rows), "segment_minutes": total_min, "by_status": by_status,
        "progress": (done_min / total_min) if total_min else 0.0,
        "gold_sets": (await db.execute(select(func.count()).select_from(m.GoldSet).where(m.GoldSet.dataset_id == dataset_id))).scalar(),
    }


def cleanup_tmp(c) -> None:
    shutil.rmtree(c.settings.data_dir / "tmp", ignore_errors=True)
