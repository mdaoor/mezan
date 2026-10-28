"""Durable job queue on the database + worker loop.

Why DB-backed: runs are idempotent per segment (hypotheses are unique per run+segment), so the only state a
queue must hold is "this job should be executing". A Postgres/SQLite row with a heartbeat gives crash recovery
without extra infrastructure. The same loop runs embedded in the API (default) or standalone via `mizan worker`.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy import select, update

from mizan.core import models as m
from mizan.core.db import utcnow

log = logging.getLogger("mizan.jobs")

QUEUES = {"ingest": {"ingest", "segment", "import", "export"}, "infer": {"run"}, "score": {"rescore"},
          "load": {"load_run"}, "report": {"report"}}
HEARTBEAT_S = 5
STALE_S = 30

Handler = Callable[["object", m.Job, "JobContext"], Awaitable[dict | None]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


class JobContext:
    def __init__(self, c, job_id: str):
        self.c, self.job_id = c, job_id
        self._last = 0.0

    async def progress(self, done: int, total: int, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now - self._last < 1.0:
            return
        self._last = now
        async with self.c.session() as db:
            await db.execute(update(m.Job).where(m.Job.id == self.job_id).values(progress_done=done, progress_total=total))
            await db.commit()
        await self.c.hub.publish(f"job:{self.job_id}", {"type": "progress", "done": done, "total": total})


async def enqueue(c, db, kind: str, payload: dict, user_id: str | None = None) -> m.Job:
    job = m.Job(kind=kind, payload=payload, created_by=user_id)
    db.add(job)
    await db.flush()
    return job


def wake(c) -> None:
    if c.worker:
        c.worker.wake()


class Worker:
    def __init__(self, c, slots: int = 4, queues: list[str] | None = None, poll_s: float = 0.5):
        import mizan.workers  # noqa: F401  (registers handlers)

        self.c, self.slots, self.poll_s = c, slots, poll_s
        self.kinds = set().union(*(QUEUES[q] for q in (queues or QUEUES))) if queues else None
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._main: asyncio.Task | None = None
        c.worker = self

    # ---------------------------------------------------------------- control
    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self._main = asyncio.create_task(self.run_forever(), name="mizan-worker")

    async def stop(self) -> None:
        self._stop.set()
        self.wake()
        if self._main:
            await asyncio.wait([self._main], timeout=10)
        for t in list(self._tasks):
            t.cancel()  # job goes back to `queued` (see _execute) and resumes on next start
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def run_forever(self) -> None:
        await self.requeue_stale()
        last_sweep = 0.0
        while not self._stop.is_set():
            claimed = False
            while len(self._tasks) < self.slots:
                job = await self._claim()
                if job is None:
                    break
                claimed = True
                t = asyncio.create_task(self._execute(job), name=f"job-{job.kind}-{job.id[:8]}")
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            now = asyncio.get_running_loop().time()
            if now - last_sweep > 15:
                last_sweep = now
                await self.requeue_stale()
            if not claimed:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.poll_s)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()

    async def drain(self, timeout: float = 60) -> None:
        """Test helper: wait until nothing is queued or running."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            async with self.c.session() as db:
                busy = (await db.execute(select(m.Job.id).where(m.Job.status.in_(("queued", "running"))).limit(1))).first()
            if not busy and not self._tasks:
                return
            self.wake()
            await asyncio.sleep(0.05)
        raise TimeoutError("jobs did not drain")

    # ------------------------------------------------------------------ queue
    async def requeue_stale(self) -> None:
        cutoff = utcnow() - timedelta(seconds=STALE_S)
        async with self.c.session() as db:
            res = await db.execute(update(m.Job).where(
                m.Job.status == "running", (m.Job.heartbeat_at < cutoff) | (m.Job.heartbeat_at.is_(None)))
                .values(status="queued"))
            await db.commit()
            if res.rowcount:
                log.warning("requeued %d stale job(s)", res.rowcount)

    async def _claim(self) -> m.Job | None:
        async with self.c.session() as db:
            q = select(m.Job.id).where(m.Job.status == "queued").order_by(m.Job.created_at).limit(5)
            if self.kinds is not None:
                q = q.where(m.Job.kind.in_(self.kinds))
            for (jid,) in (await db.execute(q)).all():
                res = await db.execute(update(m.Job).where(m.Job.id == jid, m.Job.status == "queued")
                                       .values(status="running", heartbeat_at=utcnow()))
                await db.commit()
                if res.rowcount == 1:
                    return await db.get(m.Job, jid)
        return None

    async def _beat(self, jid: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            async with self.c.session() as db:
                await db.execute(update(m.Job).where(m.Job.id == jid).values(heartbeat_at=utcnow()))
                await db.commit()

    async def _execute(self, job: m.Job) -> None:
        beat = asyncio.create_task(self._beat(job.id))
        status, result, error = "succeeded", {}, None
        try:
            fn = HANDLERS.get(job.kind)
            if fn is None:
                raise RuntimeError(f"no handler for job kind '{job.kind}'")
            result = await fn(self.c, job, JobContext(self.c, job.id)) or {}
            if result.pop("_cancelled", False):
                status = "cancelled"
        except asyncio.CancelledError:
            status = "queued"  # shutdown mid-job: resume later
            raise
        except Exception as e:
            log.exception("job %s (%s) failed", job.id, job.kind)
            status, error = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        finally:
            beat.cancel()
            async with self.c.session() as db:
                vals = {"status": status, "result": result, "error": error}
                if status != "queued":
                    vals["finished_at"] = utcnow()
                await db.execute(update(m.Job).where(m.Job.id == job.id).values(**vals))
                await db.commit()
            if status != "queued":
                await self.c.hub.publish(f"job:{job.id}", {"type": "finished", "status": status, "error": error})
