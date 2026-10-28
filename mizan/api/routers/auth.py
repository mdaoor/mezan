from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api.deps import container, current_user, get_db
from mizan.api.schemas import LoginIn
from mizan.core import models as m
from mizan.core.security import make_token, verify_password
from mizan.core.services.common import Actor
from mizan.core.services.team import user_view

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
async def login(body: LoginIn, c=Depends(container), db: AsyncSession = Depends(get_db)):
    u = (await db.execute(select(m.User).where(m.User.email == body.email.lower()))).scalar_one_or_none()
    if u is None or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "invalid credentials")
    return {"access_token": make_token(c.settings.secret, u.id, u.role, c.settings.token_ttl_h), "token_type": "bearer",
            "user": user_view(u)}


@router.get("/me")
async def me(actor: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return user_view(await db.get(m.User, actor.id))
