"""Event hub. Topics: run:{id}, load-run:{id}, dataset:{id}, segment:{id}.
In-process fan-out; if MIZAN_REDIS_URL is set events also cross processes (worker -> api) via Redis pub/sub."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict

log = logging.getLogger("mizan.events")


class Hub:
    def __init__(self, redis_url: str = ""):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._redis_url = redis_url
        self._origin = uuid.uuid4().hex
        self._redis = None
        self._task: asyncio.Task | None = None
        self.presence: dict[str, dict[str, dict]] = defaultdict(dict)

    async def start(self) -> None:
        if not self._redis_url:
            return
        try:  # pragma: no cover - needs a live redis
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self._redis_url)
            self._task = asyncio.create_task(self._listen())
        except Exception as e:
            log.warning("redis event bridge disabled: %s", e)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._redis:
            await self._redis.aclose()

    def subscribe(self, topic: str, q: asyncio.Queue) -> None:
        self._subs[topic].add(q)

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        self._subs[topic].discard(q)

    def _local(self, topic: str, event: dict) -> None:
        for q in list(self._subs.get(topic, ())):
            try:
                q.put_nowait({"topic": topic, **event})
            except asyncio.QueueFull:  # slow consumer: drop rather than block the producer
                pass

    async def publish(self, topic: str, event: dict) -> None:
        self._local(topic, event)
        if self._redis:  # pragma: no cover
            try:
                await self._redis.publish("mizan:events", json.dumps({"o": self._origin, "t": topic, "e": event}))
            except Exception as e:
                log.debug("redis publish failed: %s", e)

    async def _listen(self) -> None:  # pragma: no cover
        ps = self._redis.pubsub()
        await ps.subscribe("mizan:events")
        async for m in ps.listen():
            if m["type"] != "message":
                continue
            d = json.loads(m["data"])
            if d["o"] != self._origin:
                self._local(d["t"], d["e"])

    # -- presence (who is annotating what)
    def set_presence(self, dataset_id: str, user: dict, segment_id: str | None) -> list[dict]:
        if segment_id is None:
            self.presence[dataset_id].pop(user["id"], None)
        else:
            self.presence[dataset_id][user["id"]] = {"user_id": user["id"], "name": user["name"], "segment_id": segment_id}
        return list(self.presence[dataset_id].values())
