from __future__ import annotations

from typing import AsyncIterator

import jwt
from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.container import Container
from mizan.core.security import decode_token, role_at_least
from mizan.core.services.common import Actor


def container(request: Request) -> Container:
    return request.app.state.c


async def get_db(c: Container = Depends(container)) -> AsyncIterator[AsyncSession]:
    async with c.session() as db:
        yield db


async def current_user(request: Request, authorization: str | None = Header(default=None),
                       token: str | None = Query(default=None), c: Container = Depends(container),
                       db: AsyncSession = Depends(get_db)) -> Actor:
    raw = token
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:]
    if not raw:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_token(c.settings.secret, raw)
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from None
    u = await db.get(m.User, claims["sub"])
    if u is None:
        raise HTTPException(401, "unknown user")
    return Actor(u.id, u.role, u.name)  # role from the DB, not the token: demotions apply immediately


def require(minimum: str):
    async def dep(actor: Actor = Depends(current_user)) -> Actor:
        if not role_at_least(actor.role, minimum):
            raise HTTPException(403, f"{minimum} role required")
        return actor
    return dep


annotator = Depends(require("annotator"))
reviewer = Depends(require("reviewer"))
admin = Depends(require("admin"))
