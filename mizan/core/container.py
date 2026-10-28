from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mizan.config import Settings
from mizan.core.blobstore import LocalBlobStore
from mizan.core.db import Base, make_engine, make_sessionmaker
from mizan.core.events import Hub
from mizan.core import models as m
from mizan.core.security import hash_password
from mizan.plugins import registry as plugins


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    blobs: LocalBlobStore
    hub: Hub
    worker: Any = None

    def session(self) -> AsyncSession:
        return self.sessions()


def build_container(settings: Settings | None = None) -> Container:
    s = settings or Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    plugins.load_plugins(s.plugins_file)
    eng = make_engine(s.database_url)
    return Container(s, eng, make_sessionmaker(eng), LocalBlobStore(s.blob_dir), Hub(s.redis_url))


async def init_storage(c: Container) -> None:
    """Create tables (dev/tests; production uses `alembic upgrade head`), seed admin, sync engine kinds."""
    if c.settings.auto_create_tables:
        async with c.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async with c.session() as db:
        if (await db.execute(select(m.User.id).limit(1))).first() is None:
            db.add(m.User(email=c.settings.admin_email, name="Admin", role="admin",
                          password_hash=hash_password(c.settings.admin_password)))
        await sync_engines(db)
        await db.commit()


async def sync_engines(db: AsyncSession) -> None:
    """Mirror the adapter registry into the engines table so a new plugin shows up with no UI/schema change (N1)."""
    from mizan.core.services.registry import adapter_capabilities  # local import: avoids cycle

    existing = {e.kind: e for e in (await db.execute(select(m.Engine))).scalars()}
    for kind, cls in plugins.adapters.items():
        caps = adapter_capabilities(cls)
        e = existing.get(kind) or m.Engine(kind=kind, name=kind)
        e.name = (cls.__doc__ or kind).strip().splitlines()[0][:250]
        e.version = cls.version
        e.capabilities = caps
        e.external = bool(caps.get("external"))
        db.add(e)
