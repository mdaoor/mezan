from __future__ import annotations

import asyncio

from sqlalchemy import select


class StatusControl:
    """Watches a row's ``status`` so cancel/pause from the API (or another process) is honoured promptly."""

    def __init__(self, c, model, row_id: str):
        self.c, self.model, self.row_id = c, model, row_id
        self.state = "running"
        self._resume = asyncio.Event()
        self._resume.set()
        self._task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            async with self.c.session() as db:
                st = (await db.execute(select(self.model.status).where(self.model.id == self.row_id))).scalar_one_or_none()
            self.state = st or "cancelled"
            (self._resume.set if self.state != "paused" else self._resume.clear)()
            await asyncio.sleep(0.5)

    async def checkpoint(self) -> bool:
        """False once cancelled; blocks while paused."""
        await self._resume.wait()
        return self.state not in ("cancelled", "failed")

    @property
    def cancelled(self) -> bool:
        return self.state in ("cancelled", "failed")

    def close(self) -> None:
        self._task.cancel()
