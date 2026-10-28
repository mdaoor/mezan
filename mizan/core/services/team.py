"""Users and contribution dashboard (F16). Per-user error rates are never exposed publicly (9.8)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.errors import Conflict, Invalid, NotFound
from mizan.core.security import ROLES, hash_password
from mizan.core.services.common import Actor, audit, must_get
from mizan.core.services import gold as goldsvc

SUBMITTED_OR_LATER = ("submitted", "in_review", "changes_requested", "approved", "superseded")


def user_view(u: m.User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "leaderboard_opt_in": u.leaderboard_opt_in,
            "created_at": u.created_at}


async def create_user(db: AsyncSession, actor: Actor, email: str, name: str, role: str, password: str) -> m.User:
    if role not in ROLES:
        raise Invalid(f"role must be one of {ROLES}")
    if (await db.execute(select(m.User.id).where(m.User.email == email.lower()))).first():
        raise Conflict("email already registered")
    if len(password) < 6:
        raise Invalid("password must be at least 6 characters")
    u = m.User(email=email.lower(), name=name, role=role, password_hash=hash_password(password))
    db.add(u)
    await db.flush()
    audit(db, actor, "user.create", "user", u.id, {"role": role})
    return u


async def patch_user(db: AsyncSession, actor: Actor, user_id: str, role: str | None, name: str | None,
                     password: str | None, leaderboard_opt_in: bool | None) -> m.User:
    u = await must_get(db, m.User, user_id, "User")
    if role is not None:
        if role not in ROLES:
            raise Invalid(f"role must be one of {ROLES}")
        if u.id == actor.id and role != "admin":
            raise Conflict("admins cannot demote themselves")
        u.role = role
    if name:
        u.name = name
    if password:
        u.password_hash = hash_password(password)
    if leaderboard_opt_in is not None:
        u.leaderboard_opt_in = leaderboard_opt_in
    audit(db, actor, "user.patch", "user", u.id, {"role": role})
    return u


def _streak(days: set) -> int:
    d = datetime.now(timezone.utc).date()
    if d not in days:
        d -= timedelta(days=1)  # today may not have activity yet
    n = 0
    while d in days:
        n, d = n + 1, d - timedelta(days=1)
    return n


async def contributions(db: AsyncSession, actor: Actor, project_id: str | None = None, since: datetime | None = None) -> dict:
    users = (await db.execute(select(m.User).order_by(m.User.name))).scalars().all()
    q = (select(m.Annotation, m.Segment).join(m.Segment, m.Segment.id == m.Annotation.segment_id)
         .where(m.Annotation.track == "primary", m.Annotation.author_id.is_not(None), m.Annotation.status.in_(SUBMITTED_OR_LATER)))
    if project_id:
        q = (q.join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id).join(m.Dataset, m.Dataset.id == m.AudioFile.dataset_id)
             .where(m.Dataset.project_id == project_id))
    if since:
        q = q.where(m.Annotation.updated_at >= since)
    ann = (await db.execute(q)).all()
    aq = select(m.AuditLog).where(m.AuditLog.action.in_(("annotation.approve", "annotation.request_changes")))
    if since:
        aq = aq.where(m.AuditLog.ts >= since)
    reviews = (await db.execute(aq)).scalars().all()
    # iaa per user
    iaa_by_user: dict[str, float] = {}
    for ds in (await db.execute(select(m.Dataset.id).where(*( [m.Dataset.project_id == project_id] if project_id else [])))).scalars():
        r = await goldsvc.compute_iaa(db, ds)
        for pa in r["per_annotator"]:
            if pa["agreement"] is not None:
                iaa_by_user[pa["user_id"]] = pa["agreement"]
    out = []
    for u in users:
        mine = [(a, s) for a, s in ann if a.author_id == u.id]
        seg_seen: dict[str, float] = {}
        for a, s in mine:
            seg_seen[s.id] = s.end_s - s.start_s
        days = {a.updated_at.date() for a, _ in mine} | {r.ts.date() for r in reviews if r.actor_id == u.id}
        done = [r for r in reviews if r.actor_id == u.id]
        outcomes = [r for r in reviews if r.diff.get("author") == u.id]
        approved = sum(r.action == "annotation.approve" for r in outcomes)
        row = {"user_id": u.id, "name": u.name, "role": u.role, "minutes_annotated": round(sum(seg_seen.values()) / 60, 2),
               "segments_annotated": len(seg_seen), "segments_reviewed": len(done), "streak_days": _streak(days),
               "leaderboard_opt_in": u.leaderboard_opt_in,
               "approval_rate": (approved / len(outcomes)) if outcomes else None, "iaa": iaa_by_user.get(u.id)}
        out.append(row)
    private = ("approval_rate", "iaa")  # quality signals: visible to self, reviewers (tooltip) and admins only
    shown = []
    for row in out:
        mine = row["user_id"] == actor.id
        if mine or actor.role in ("admin", "reviewer"):   # reviewers see quality signals (tooltip), never the public
            shown.append(row)
        elif row["leaderboard_opt_in"]:
            shown.append({k: (None if k in private else v) for k, v in row.items()})
    board = sorted((r for r in out if r["leaderboard_opt_in"]), key=lambda r: -r["minutes_annotated"])
    return {"users": shown, "leaderboard": [{k: r[k] for k in ("user_id", "name", "minutes_annotated", "segments_reviewed", "streak_days")} for r in board]}
