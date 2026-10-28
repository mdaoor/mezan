"""Annotation workflow (spec 4.3 state machines, 6.2, 9.3, 9.4)."""
from __future__ import annotations

import hashlib
import math
import random
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.db import utcnow
from mizan.core.errors import Conflict, Forbidden, Invalid, NotFound
from mizan.core.security import role_at_least
from mizan.core.services.common import Actor, audit, must_get
from mizan.core.services.datasets import AUTO_STRATA, STANDARD_FLAGS
from mizan.plugins.textutil import infer_spans, lang_mix, nfc

CLAIMABLE = ("draft", "changes_requested")
LOCKED = ("submitted", "in_review", "approved", "superseded")
NO_TEXT_OK_FLAGS = {"no_speech", "music", "unintelligible"}


# ----------------------------------------------------------------- helpers
async def latest(db: AsyncSession, segment_id: str, track: str = "primary", author_id: str | None = None) -> m.Annotation | None:
    q = select(m.Annotation).where(m.Annotation.segment_id == segment_id, m.Annotation.track == track)
    if author_id:
        q = q.where(m.Annotation.author_id == author_id)
    return (await db.execute(q.order_by(m.Annotation.version.desc()).limit(1))).scalar_one_or_none()


async def _next_version(db: AsyncSession, segment_id: str) -> int:
    return ((await db.execute(select(func.max(m.Annotation.version)).where(m.Annotation.segment_id == segment_id))).scalar() or 0) + 1


def _active_claim(a: m.Annotation, now) -> bool:
    return bool(a.claimed_by and a.claim_expires_at and a.claim_expires_at > now)


def annotation_view(a: m.Annotation) -> dict:
    return {"id": a.id, "segment_id": a.segment_id, "version": a.version, "track": a.track, "text": a.text,
            "spans": a.spans, "status": a.status, "source": a.source, "note": a.note, "author_id": a.author_id,
            "reviewer_id": a.reviewer_id, "claimed_by": a.claimed_by, "claim_expires_at": a.claim_expires_at,
            "created_at": a.created_at, "updated_at": a.updated_at}


async def _publish(c, segment: m.Segment, dataset_id: str | None, event: dict) -> None:
    await c.hub.publish(f"segment:{segment.id}", event)
    if dataset_id:
        await c.hub.publish(f"dataset:{dataset_id}", {**event, "segment_id": segment.id})


async def dataset_of_segment(db: AsyncSession, seg: m.Segment) -> str:
    return (await db.execute(select(m.AudioFile.dataset_id).where(m.AudioFile.id == seg.audio_file_id))).scalar_one()


# ------------------------------------------------------------------ claims
async def claim_batch(c, db: AsyncSession, actor: Actor, dataset_id: str, minutes: float, strategy: str = "oldest_unclaimed",
                      dialect: str | None = None) -> dict:
    await must_get(db, m.Dataset, dataset_id, "Dataset")
    if strategy not in ("oldest_unclaimed", "random", "my_dialect", "iaa"):
        raise Invalid(f"unknown strategy '{strategy}'")
    now = utcnow()
    exp = now + timedelta(minutes=c.settings.claim_ttl_min)
    track = "iaa" if strategy == "iaa" else "primary"
    budget = minutes * 60

    claimed: list[dict] = []
    total = 0.0
    taken: set[str] = set()
    for _pass in range(5):  # re-query after losing races so concurrent claimers still fill their budget
        cands, created = await _candidates(db, actor, dataset_id, track, strategy, dialect, now)
        cands = [t for t in cands if t[0].id not in taken]
        if not cands or total >= budget:
            break
        lost = False
        for seg, ann in cands:
            if total >= budget:
                break
            dur = seg.end_s - seg.start_s
            if track == "iaa":
                try:
                    async with db.begin_nested():
                        row = m.Annotation(segment_id=seg.id, version=await _next_version(db, seg.id), track="iaa",
                                           text="", status="draft", source="human", author_id=actor.id,
                                           claimed_by=actor.id, claim_expires_at=exp)
                        db.add(row)
                        await db.flush()
                except IntegrityError:
                    lost = True
                    continue
                ann = row
            elif ann is None:
                try:
                    async with db.begin_nested():
                        ann = m.Annotation(segment_id=seg.id, version=1, text="", status="draft", source="human",
                                           claimed_by=actor.id, claim_expires_at=exp)
                        db.add(ann)
                        await db.flush()
                except IntegrityError:
                    lost = True  # someone else created v1 first
                    continue
            else:
                res = await db.execute(update(m.Annotation).where(
                    m.Annotation.id == ann.id, m.Annotation.status.in_(CLAIMABLE),
                    or_(m.Annotation.claimed_by.is_(None), m.Annotation.claimed_by == actor.id, m.Annotation.claim_expires_at < now))
                    .values(claimed_by=actor.id, claim_expires_at=exp))
                if res.rowcount != 1:
                    lost = True  # lost the race
                    continue
                await db.refresh(ann)
            taken.add(seg.id)
            claimed.append({"segment": seg, "annotation": ann})
            total += dur
        if not lost:
            break
        await db.commit()  # make our claims visible before re-querying
    audit(db, actor, "claims.create", "dataset", dataset_id, {"segments": len(claimed), "seconds": total, "strategy": strategy})
    await db.commit()
    if claimed:
        await c.hub.publish(f"dataset:{dataset_id}", {"type": "claimed", "user_id": actor.id, "name": actor.name, "count": len(claimed)})
    return {"expires_at": exp, "seconds": total, "track": track,
            "segments": [_claim_item(x["segment"], x["annotation"]) for x in claimed],
            "segment_ids": [x["segment"].id for x in claimed]}


async def _candidates(db: AsyncSession, actor: Actor, dataset_id: str, track: str, strategy: str, dialect: str | None, now):
    latest_v = select(m.Annotation.segment_id, func.max(m.Annotation.version).label("v")).where(
        m.Annotation.track == "primary").group_by(m.Annotation.segment_id).subquery()
    q = (select(m.Segment, m.Annotation, m.AudioFile.created_at)
         .join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .outerjoin(latest_v, latest_v.c.segment_id == m.Segment.id)
         .outerjoin(m.Annotation, and_(m.Annotation.segment_id == m.Segment.id, m.Annotation.version == latest_v.c.v,
                                       m.Annotation.track == "primary"))
         .where(m.AudioFile.dataset_id == dataset_id))
    rows = (await db.execute(q.execution_options(populate_existing=True))).all()  # re-read: other claimers commit meanwhile
    cands: list[tuple[m.Segment, m.Annotation | None]] = []
    if track == "iaa":
        mine = set((await db.execute(select(m.Annotation.segment_id).where(
            m.Annotation.track == "iaa", m.Annotation.author_id == actor.id))).scalars())
        for seg, ann, _ in rows:
            if seg.strata.get("iaa") and ann is not None and ann.status == "approved" and ann.author_id != actor.id and seg.id not in mine:
                cands.append((seg, ann))
    else:
        for seg, ann, _ in rows:
            if ann is None or (ann.status in CLAIMABLE and (not _active_claim(ann, now) or ann.claimed_by == actor.id)):
                if ann is not None and _active_claim(ann, now) and ann.claimed_by == actor.id:
                    continue  # already in my batch
                cands.append((seg, ann))
    created = {r[0].id: r[2] for r in rows}
    prio = lambda s: -int(s.strata.get("priority", 0) or 0)  # noqa: E731
    if strategy == "random":
        random.shuffle(cands)
        cands.sort(key=lambda t: prio(t[0]))
    else:
        mine_first = lambda t: 0 if (t[1] is not None and t[1].author_id == actor.id and t[1].status == "changes_requested") else 1  # noqa: E731
        cands.sort(key=lambda t: (prio(t[0]), mine_first(t), created[t[0].id], t[0].audio_file_id, t[0].idx))
        if strategy == "my_dialect" and dialect:
            cands.sort(key=lambda t: 0 if t[0].strata.get("dialect") == dialect else 1)  # stable
    return cands, created


def _claim_item(seg: m.Segment, a: m.Annotation) -> dict:
    return {"id": seg.id, "audio_file_id": seg.audio_file_id, "index": seg.idx, "start_s": seg.start_s, "end_s": seg.end_s,
            "duration_s": seg.end_s - seg.start_s, "flags": seg.flags, "strata": seg.strata,
            "annotation_id": a.id, "status": a.status, "track": a.track, "source": a.source,
            "has_text": bool(a.text.strip()), "claim_expires_at": a.claim_expires_at}


async def my_claims(c, db: AsyncSession, actor: Actor, dataset_id: str) -> dict:
    now = utcnow()
    q = (select(m.Segment, m.Annotation).join(m.Annotation, m.Annotation.segment_id == m.Segment.id)
         .join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .where(m.AudioFile.dataset_id == dataset_id, m.Annotation.claimed_by == actor.id, m.Annotation.claim_expires_at > now)
         .order_by(m.AudioFile.created_at, m.Segment.audio_file_id, m.Segment.idx))
    rows = (await db.execute(q)).all()
    # an old version row may still carry the claim only if a newer one doesn't; keep the latest per (segment, track)
    best: dict[tuple, tuple] = {}
    for s, a in rows:
        k = (s.id, a.track)
        if k not in best or a.version > best[k][1].version:
            best[k] = (s, a)
    items = [_claim_item(s, a) for s, a in best.values() if a.claimed_by == actor.id]
    sec = sum(i["duration_s"] for i in items if i["status"] in CLAIMABLE)
    return {"segments": items, "seconds_left": sec}


async def release(c, db: AsyncSession, actor: Actor, segment_id: str) -> dict:
    seg = await must_get(db, m.Segment, segment_id, "Segment")
    n = 0
    for track in ("primary", "iaa"):
        a = await latest(db, segment_id, track, author_id=actor.id if track == "iaa" else None)
        if a and a.claimed_by:  # spec F4: anyone can release a claim
            a.claimed_by, a.claim_expires_at = None, None
            n += 1
            if track == "iaa" and not a.text.strip():
                await db.delete(a)  # blank double-annotation placeholder
    audit(db, actor, "claim.release", "segment", segment_id)
    await db.commit()
    await _publish(c, seg, await dataset_of_segment(db, seg), {"type": "released", "user_id": actor.id})
    return {"released": n}


async def extend_claim(c, db: AsyncSession, actor: Actor, segment_id: str, minutes: int | None = None) -> dict:
    a = await latest(db, segment_id)
    if not a or a.claimed_by != actor.id:
        raise Conflict("you do not hold this claim")
    a.claim_expires_at = utcnow() + timedelta(minutes=minutes or c.settings.claim_ttl_min)
    await db.commit()
    return {"claim_expires_at": a.claim_expires_at}


# ----------------------------------------------------------------- working
async def get_working(db: AsyncSession, actor: Actor, segment_id: str, track: str = "primary") -> dict:
    seg = await must_get(db, m.Segment, segment_id, "Segment")
    if track == "iaa":  # blind: never expose the primary text
        cur = await latest(db, segment_id, "iaa", author_id=actor.id)
        hist = [cur] if cur else []
    else:
        cur = await latest(db, segment_id)
        hist = (await db.execute(select(m.Annotation).where(m.Annotation.segment_id == segment_id, m.Annotation.track == "primary")
                                 .order_by(m.Annotation.version.desc()))).scalars().all()
    comments = []
    if track == "primary" and hist:
        comments = (await db.execute(select(m.Comment).where(m.Comment.annotation_id.in_([h.id for h in hist]))
                                     .order_by(m.Comment.created_at))).scalars().all()
    draft = next((h for h in reversed(hist) if h.source.startswith("machine:")), None)
    return {"segment": {"id": seg.id, "audio_file_id": seg.audio_file_id, "start_s": seg.start_s, "end_s": seg.end_s,
                        "flags": seg.flags, "strata": seg.strata, "index": seg.idx},
            "annotation": annotation_view(cur) if cur else None,
            "history": [annotation_view(h) for h in hist],
            "machine_draft": {"text": draft.text, "source": draft.source} if draft else None,
            "comments": [comment_view(x) for x in comments]}


def comment_view(x: m.Comment) -> dict:
    return {"id": x.id, "annotation_id": x.annotation_id, "parent_id": x.parent_id, "author_id": x.author_id,
            "body": x.body, "resolved": x.resolved, "created_at": x.created_at}


def _clean_spans(text: str, spans: list[dict] | None) -> list[dict]:
    if not spans:
        return infer_spans(text)
    out = []
    for s in spans:
        a, b, lang = int(s["start_char"]), int(s["end_char"]), s.get("lang")
        if lang not in ("ar", "en", "other") or not (0 <= a < b <= len(text)):
            raise Invalid(f"invalid span {s}", text_len=len(text))
        out.append({"start_char": a, "end_char": b, "lang": lang})
    return sorted(out, key=lambda s: s["start_char"])


async def save(c, db: AsyncSession, actor: Actor, segment_id: str, text: str, spans: list[dict] | None = None,
               flags: list[str] | None = None, note: str | None = None, strata: dict | None = None,
               track: str = "primary") -> dict:
    if not role_at_least(actor.role, "annotator"):
        raise Forbidden("viewers cannot annotate")
    seg = await must_get(db, m.Segment, segment_id, "Segment")
    now = utcnow()
    text = nfc(text)
    cur = await latest(db, segment_id, track, author_id=actor.id if track == "iaa" else None)

    if track == "iaa":
        if cur is None or cur.claimed_by != actor.id or cur.status != "draft":
            raise Conflict("claim this segment for double annotation first")
    elif cur is not None:
        if cur.status in LOCKED:
            raise Conflict(f"annotation is {cur.status}; ask a reviewer to reopen it", status=cur.status)
        if _active_claim(cur, now) and cur.claimed_by != actor.id:
            raise Conflict("segment is claimed by someone else", claimed_by=cur.claimed_by, expires_at=cur.claim_expires_at)

    if flags is not None:
        bad = set(flags) - set(STANDARD_FLAGS)
        if bad:
            raise Invalid(f"unknown flags {sorted(bad)}", allowed=STANDARD_FLAGS)
        seg.flags = sorted(set(flags))
    if strata:
        seg.strata = {**seg.strata, **{k: v for k, v in strata.items() if k not in AUTO_STRATA}}

    exp = (cur.claim_expires_at if cur and _active_claim(cur, now) and cur.claimed_by == actor.id
           else now + timedelta(minutes=c.settings.claim_ttl_min))
    machine = cur is not None and cur.source.startswith("machine:")
    source = cur.source if (machine and cur.text == text) else ("import" if cur and cur.source == "import" and cur.text == text else "human")
    row = m.Annotation(segment_id=segment_id, version=await _next_version(db, segment_id), track=track, text=text,
                       spans=_clean_spans(text, spans), status="draft", source=source,
                       note=note if note is not None else (cur.note if cur else ""),
                       author_id=actor.id, claimed_by=actor.id, claim_expires_at=exp)
    if cur is not None:
        cur.claimed_by, cur.claim_expires_at = None, None
    db.add(row)
    await db.commit()
    return annotation_view(row)


def _check_submittable(text: str, flags: list[str]) -> None:
    if not text.strip() and not (NO_TEXT_OK_FLAGS & set(flags)):
        raise Invalid("empty transcript: add text or flag the segment (no_speech / music / unintelligible)")


async def submit(c, db: AsyncSession, actor: Actor, segment_id: str, track: str = "primary") -> dict:
    seg = await must_get(db, m.Segment, segment_id, "Segment")
    cur = await latest(db, segment_id, track, author_id=actor.id if track == "iaa" else None)
    if cur is None:
        raise NotFound("nothing to submit")
    if cur.status != "draft":
        raise Conflict(f"annotation is {cur.status}, expected draft")
    if cur.claimed_by != actor.id and not (cur.author_id == actor.id) and actor.role != "admin":
        raise Forbidden("claim this segment first")
    _check_submittable(cur.text, seg.flags)
    cur.status, cur.author_id = "submitted", actor.id
    cur.claimed_by, cur.claim_expires_at = None, None
    if track == "primary":
        seg.strata = {**seg.strata, "lang_mix": lang_mix(cur.text, cur.spans)}
    audit(db, actor, "annotation.submit", "segment", segment_id, {"version": cur.version, "track": track})
    await db.commit()
    ds = await dataset_of_segment(db, seg)
    await _publish(c, seg, ds, {"type": "submitted", "user_id": actor.id, "name": actor.name})
    return annotation_view(cur)


async def reopen(c, db: AsyncSession, actor: Actor, segment_id: str) -> dict:
    """Reviewer/admin re-opens a locked annotation as a fresh draft; the approved one stays until superseded."""
    if not role_at_least(actor.role, "reviewer"):
        raise Forbidden("reviewer role required")
    cur = await latest(db, segment_id)
    if cur is None or cur.status not in ("approved", "submitted", "in_review"):
        raise Conflict("nothing to reopen")
    row = m.Annotation(segment_id=segment_id, version=await _next_version(db, segment_id), text=cur.text, spans=cur.spans,
                       status="draft", source="human", author_id=None,
                       claimed_by=actor.id, claim_expires_at=utcnow() + timedelta(minutes=c.settings.claim_ttl_min))
    cur.claimed_by = None
    db.add(row)
    audit(db, actor, "annotation.reopen", "segment", segment_id, {"from": cur.version})
    await db.commit()
    return annotation_view(row)


async def queue_segment(c, db: AsyncSession, actor: Actor, segment_id: str, priority: int = 1) -> dict:
    """Disagreement explorer -> 'send to annotation queue': priority claim ordering + reopen if already approved."""
    seg = await must_get(db, m.Segment, segment_id, "Segment")
    seg.strata = {**seg.strata, "priority": priority}
    cur = await latest(db, segment_id)
    if cur is not None and cur.status in ("approved", "submitted", "in_review"):
        db.add(m.Annotation(segment_id=segment_id, version=await _next_version(db, segment_id), text=cur.text, spans=cur.spans,
                            status="draft", source="human"))
    audit(db, actor, "segment.queue", "segment", segment_id, {"priority": priority})
    await db.commit()
    return {"segment_id": segment_id, "priority": priority}


# ------------------------------------------------------------------ review
async def review(c, db: AsyncSession, actor: Actor, segment_id: str, action: str, note: str | None = None,
                 text: str | None = None, spans: list[dict] | None = None) -> dict:
    if not role_at_least(actor.role, "reviewer"):
        raise Forbidden("reviewer role required")
    if action not in ("approve", "request_changes", "start"):
        raise Invalid("action must be approve | request_changes | start")
    seg = await must_get(db, m.Segment, segment_id, "Segment")
    cur = await latest(db, segment_id)
    if cur is None or cur.status not in ("submitted", "in_review"):
        raise Conflict(f"nothing to review (status: {cur.status if cur else 'none'})")
    if cur.author_id == actor.id:
        raise Forbidden("reviewer must differ from the author")
    if cur.status == "in_review" and cur.reviewer_id not in (None, actor.id) and action == "start":
        raise Conflict("another reviewer already started this review")

    if action == "start":
        cur.status, cur.reviewer_id = "in_review", actor.id
        out = cur
    elif action == "request_changes":
        if not (note or "").strip():
            raise Invalid("a comment is required when requesting changes")
        cur.status, cur.reviewer_id = "changes_requested", actor.id
        db.add(m.Comment(annotation_id=cur.id, author_id=actor.id, body=note.strip()))
        cur.claimed_by = cur.author_id  # back to the author's batch
        cur.claim_expires_at = utcnow() + timedelta(minutes=c.settings.claim_ttl_min)
        out = cur
    else:
        out = cur
        if text is not None and nfc(text) != cur.text:  # edit & approve: reviewer becomes co-author
            out = m.Annotation(segment_id=segment_id, version=await _next_version(db, segment_id), text=nfc(text),
                               spans=_clean_spans(nfc(text), spans), source="human", author_id=cur.author_id,
                               note=cur.note, status="approved")
            db.add(out)
            cur.status = "superseded"
        out.status, out.reviewer_id = "approved", actor.id
        out.claimed_by, out.claim_expires_at = None, None
        await db.flush()
        await db.execute(update(m.Annotation).where(
            m.Annotation.segment_id == segment_id, m.Annotation.status == "approved", m.Annotation.id != out.id,
            m.Annotation.track == "primary").values(status="superseded"))
        seg.strata = {**seg.strata, "lang_mix": lang_mix(out.text, out.spans)}
        if note and note.strip():
            db.add(m.Comment(annotation_id=out.id, author_id=actor.id, body=note.strip()))
    audit(db, actor, f"annotation.{action}", "segment", segment_id, {"version": out.version, "author": cur.author_id})
    await db.commit()
    ds = await dataset_of_segment(db, seg)
    await _publish(c, seg, ds, {"type": {"approve": "approved", "request_changes": "changes_requested", "start": "review_started"}[action],
                               "user_id": actor.id, "name": actor.name})
    return annotation_view(out)


def spot_check_ids(ids: list[str], pct: float = 0.10) -> list[str]:
    """Deterministic 10% (min 1) sample that must be listened to before a batch approve."""
    if not ids:
        return []
    k = max(1, math.ceil(len(ids) * pct))
    return sorted(sorted(ids, key=lambda i: hashlib.sha1((i + ":spot").encode()).hexdigest())[:k])


async def batch_approve(c, db: AsyncSession, actor: Actor, segment_ids: list[str], listened_ids: list[str]) -> dict:
    need = set(spot_check_ids(segment_ids))
    if not need <= set(listened_ids):
        raise Invalid("spot-check required: listen to the sampled segments before batch approving",
                      spot_check_ids=sorted(need - set(listened_ids)))
    done, failed = [], []
    for sid in segment_ids:
        try:
            await review(c, db, actor, sid, "approve")
            done.append(sid)
        except Exception as e:
            await db.rollback()
            failed.append({"segment_id": sid, "error": getattr(e, "message", str(e))})
    return {"approved": done, "failed": failed, "spot_check_ids": sorted(need)}


async def review_queue(db: AsyncSession, actor: Actor, dataset_id: str | None = None, annotator_id: str | None = None,
                       dialect: str | None = None, flag: str | None = None, has_comments: bool | None = None,
                       limit: int = 50, offset: int = 0) -> dict:
    latest_v = select(m.Annotation.segment_id, func.max(m.Annotation.version).label("v")).where(
        m.Annotation.track == "primary").group_by(m.Annotation.segment_id).subquery()
    q = (select(m.Segment, m.Annotation, m.AudioFile.dataset_id)
         .join(latest_v, latest_v.c.segment_id == m.Segment.id)
         .join(m.Annotation, and_(m.Annotation.segment_id == m.Segment.id, m.Annotation.version == latest_v.c.v))
         .join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .where(m.Annotation.status.in_(("submitted", "in_review")), m.Annotation.author_id != actor.id,
                m.Annotation.track == "primary")
         .order_by(m.Annotation.updated_at))
    if dataset_id:
        q = q.where(m.AudioFile.dataset_id == dataset_id)
    if annotator_id:
        q = q.where(m.Annotation.author_id == annotator_id)
    rows = (await db.execute(q)).all()
    cids = {a.id for _, a, _ in rows}
    with_comments = set()
    if cids:
        with_comments = set((await db.execute(select(m.Comment.annotation_id).where(
            m.Comment.annotation_id.in_(cids), m.Comment.resolved.is_(False)))).scalars())
    items = []
    for seg, a, ds in rows:
        if dialect and seg.strata.get("dialect") != dialect:
            continue
        if flag and flag not in seg.flags:
            continue
        if has_comments is not None and (a.id in with_comments) != has_comments:
            continue
        draft = (await db.execute(select(m.Annotation.text).where(
            m.Annotation.segment_id == seg.id, m.Annotation.source.like("machine:%")).order_by(m.Annotation.version).limit(1))).scalar()
        items.append({"segment": {"id": seg.id, "audio_file_id": seg.audio_file_id, "start_s": seg.start_s, "end_s": seg.end_s,
                                  "flags": seg.flags, "strata": seg.strata, "dataset_id": ds},
                      "annotation": annotation_view(a), "machine_draft": draft, "open_comments": a.id in with_comments})
    page = items[offset: offset + limit + 1]
    return {"items": page[:limit], "next_cursor": str(offset + limit) if len(page) > limit else None, "total": len(items)}


async def author_stats(db: AsyncSession, user_id: str) -> dict:
    ap = (await db.execute(select(func.count()).select_from(m.AuditLog).where(
        m.AuditLog.action == "annotation.approve", m.AuditLog.diff["author"].as_string() == user_id))).scalar() or 0
    cr = (await db.execute(select(func.count()).select_from(m.AuditLog).where(
        m.AuditLog.action == "annotation.request_changes", m.AuditLog.diff["author"].as_string() == user_id))).scalar() or 0
    return {"user_id": user_id, "reviewed": ap + cr, "approval_rate": (ap / (ap + cr)) if ap + cr else None}


# ---------------------------------------------------------------- comments
async def add_comment(c, db: AsyncSession, actor: Actor, annotation_id: str, body: str, parent_id: str | None = None) -> dict:
    if not role_at_least(actor.role, "annotator"):
        raise Forbidden("viewers cannot comment")
    a = await must_get(db, m.Annotation, annotation_id, "Annotation")
    if parent_id:
        p = await must_get(db, m.Comment, parent_id, "Parent comment")
        if p.annotation_id != annotation_id:
            raise Invalid("parent comment belongs to another annotation")
    if not body.strip():
        raise Invalid("empty comment")
    cm = m.Comment(annotation_id=annotation_id, parent_id=parent_id, author_id=actor.id, body=body.strip())
    db.add(cm)
    await db.commit()
    seg = await db.get(m.Segment, a.segment_id)
    await c.hub.publish(f"segment:{seg.id}", {"type": "comment", "comment": comment_view(cm)})
    await c.hub.publish(f"dataset:{await dataset_of_segment(db, seg)}", {"type": "comment", "segment_id": seg.id})
    return comment_view(cm)


async def patch_comment(c, db: AsyncSession, actor: Actor, comment_id: str, resolved: bool | None, body: str | None) -> dict:
    cm = await must_get(db, m.Comment, comment_id, "Comment")
    if body is not None:
        if cm.author_id != actor.id and actor.role != "admin":
            raise Forbidden("only the author can edit a comment")
        cm.body = body.strip()
    if resolved is not None:
        cm.resolved = resolved
    await db.commit()
    a = await db.get(m.Annotation, cm.annotation_id)
    await c.hub.publish(f"segment:{a.segment_id}", {"type": "comment", "comment": comment_view(cm)})
    return comment_view(cm)
