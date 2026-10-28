from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, JSON, TypeDecorator, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """All timestamps are UTC (handover rule 5); SQLite drops tzinfo so restore it on read."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc) if value is not None else None

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONType, list: JSONType, datetime: UTCDateTime}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_engine(url: str) -> AsyncEngine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    else:
        kwargs.update(pool_size=20, max_overflow=20, pool_pre_ping=True)
    eng = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng.sync_engine, "connect")
        def _pragmas(dbapi_conn, _):  # WAL lets API readers and worker writers coexist
            dbapi_conn.isolation_level = None  # we emit BEGIN ourselves (below)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
        @event.listens_for(eng.sync_engine, "begin")
        def _begin_immediate(conn):
            # A deferred BEGIN that later upgrades read->write fails instantly with "database is locked" when another
            # writer committed meanwhile (busy_timeout does not apply). IMMEDIATE queues writers instead. Dev/CI mode only;
            # production uses Postgres.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    return eng


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
