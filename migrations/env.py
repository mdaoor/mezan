import asyncio

from alembic import context

from mizan.config import Settings
from mizan.core import models  # noqa: F401
from mizan.core.db import Base, make_engine

target_metadata = Base.metadata
url = Settings().database_url


def do_run(conn):
    context.configure(connection=conn, target_metadata=target_metadata, compare_type=True, render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    eng = make_engine(url)
    async with eng.connect() as conn:
        await conn.run_sync(do_run)
    await eng.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
