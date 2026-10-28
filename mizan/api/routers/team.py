from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api import schemas as S
from mizan.api.deps import admin, current_user, get_db, reviewer
from mizan.core import models as m
from mizan.core.services import team as svc
from mizan.core.services.common import Actor

router = APIRouter(tags=["team"])


@router.get("/team/contributions")
async def contributions(project: str | None = None, since: datetime | None = None, actor: Actor = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):
    return await svc.contributions(db, actor, project, since)


@router.get("/users")
async def list_users(_: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    return {"items": [svc.user_view(u) for u in (await db.execute(select(m.User).order_by(m.User.name))).scalars()]}


@router.post("/users", status_code=201)
async def create_user(body: S.UserIn, actor: Actor = admin, db: AsyncSession = Depends(get_db)):
    u = await svc.create_user(db, actor, body.email, body.name, body.role, body.password)
    await db.commit()
    return svc.user_view(u)


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, body: S.UserPatch, actor: Actor = admin, db: AsyncSession = Depends(get_db)):
    u = await svc.patch_user(db, actor, user_id, body.role, body.name, body.password, body.leaderboard_opt_in)
    await db.commit()
    return svc.user_view(u)


@router.patch("/me/preferences")
async def my_prefs(body: S.UserPatch, actor: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Self-service: only the leaderboard opt-in and display name."""
    u = await db.get(m.User, actor.id)
    if body.leaderboard_opt_in is not None:
        u.leaderboard_opt_in = body.leaderboard_opt_in
    if body.name:
        u.name = body.name
    await db.commit()
    return svc.user_view(u)
