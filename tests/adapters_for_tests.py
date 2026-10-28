"""Test-only engines registered into the plugin registry (proves N1: a new engine needs no UI/schema change)."""
import asyncio
import time

from mizan.plugins import registry as plugins
from mizan.plugins.base import AdapterError, AudioInput, Capabilities, EngineAdapter, TranscriptResult


class CountingAdapter(EngineAdapter):
    """Scripted engine: config.transcripts maps segment_id -> text; counts calls; can fail on demand."""
    kind = "counting"
    version = "9.9.9"
    calls: dict[str, int] = {}
    inflight = 0
    max_inflight = 0

    @classmethod
    def config_schema(cls):
        return {"type": "object", "required": ["model"], "properties": {
            "model": {"type": "string", "default": "counting"}, "base_url": {"type": "string", "default": "x://"},
            "transcripts": {"type": "object", "default": {}}, "default_text": {"type": "string", "default": "hello"},
            "latency_ms": {"type": "number", "default": 1}, "fail_first": {"type": "integer", "default": 0},
            "fail_always": {"type": "boolean", "default": False}, "fatal_on": {"type": "array", "default": []},
            "max_concurrency": {"type": "integer", "default": 8}}}

    def __init__(self, cfg):
        self.cfg = cfg

    def capabilities(self):
        return Capabilities(False, False, True, False, 30, self.cfg.get("max_concurrency", 8), False)

    async def health(self):
        return {"ok": True, "model": self.cfg["model"], "detail": ""}

    async def transcribe(self, audio: AudioInput, options):
        c = CountingAdapter
        c.calls[audio.segment_id] = c.calls.get(audio.segment_id, 0) + 1
        c.inflight += 1
        c.max_inflight = max(c.max_inflight, c.inflight)
        t0 = time.perf_counter()
        try:
            await asyncio.sleep(self.cfg.get("latency_ms", 1) / 1000)
            if self.cfg.get("fail_always"):
                raise AdapterError(True, "http_503")
            if audio.segment_id in self.cfg.get("fatal_on", []):
                raise AdapterError(False, "http_400")
            if c.calls[audio.segment_id] <= self.cfg.get("fail_first", 0):
                raise AdapterError(True, "timeout")
            text = self.cfg.get("transcripts", {}).get(audio.segment_id, self.cfg.get("default_text", "hello"))
            return TranscriptResult(text, None, (time.perf_counter() - t0) * 1000, {"text": text})
        finally:
            c.inflight -= 1

    @classmethod
    def reset(cls):
        cls.calls, cls.inflight, cls.max_inflight = {}, 0, 0


plugins.adapters.register(CountingAdapter)
