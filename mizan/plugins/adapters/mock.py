"""Deterministic in-memory engine for demos, CI and load-harness self-tests (no network, no GPU)."""
from __future__ import annotations

import asyncio
import random
import time

from mizan.plugins.base import AdapterError, AudioInput, Capabilities, EngineAdapter, TranscriptResult


class MockAdapter(EngineAdapter):
    kind = "mock"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return {"type": "object", "required": ["model"], "properties": {
            "model": {"type": "string", "default": "mock-asr"},
            "base_url": {"type": "string", "default": "mock://local"},
            "transcripts": {"type": "object", "default": {}, "description": "segment_id → text"},
            "default_text": {"type": "string", "default": "مرحبا hello"},
            "latency_ms": {"type": "number", "default": 20, "description": "Base simulated latency"},
            "latency_per_audio_s_ms": {"type": "number", "default": 0},
            "saturate_at": {"type": "integer", "default": 0, "description": "In-flight count above which latency grows"},
            "error_rate": {"type": "number", "default": 0, "minimum": 0, "maximum": 1},
            "max_concurrency": {"type": "integer", "default": 16},
            "seed": {"type": "integer", "default": 0},
        }}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._rng = random.Random(cfg.get("seed", 0))
        self._inflight = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(False, False, True, False, 30, self.cfg.get("max_concurrency", 16), False)

    async def health(self) -> dict:
        return {"ok": True, "model": self.cfg["model"], "detail": "mock"}

    async def list_models(self) -> list[str]:
        return [self.cfg["model"]]

    async def transcribe(self, audio: AudioInput, options: dict) -> TranscriptResult:
        t0 = time.perf_counter()
        self._inflight += 1
        try:
            lat = self.cfg.get("latency_ms", 20) + self.cfg.get("latency_per_audio_s_ms", 0) * audio.duration_s
            sat = self.cfg.get("saturate_at", 0)
            if sat and self._inflight > sat:
                lat *= 1 + (self._inflight - sat) * 0.5
            await asyncio.sleep(lat / 1000)
            if self._rng.random() < self.cfg.get("error_rate", 0):
                raise AdapterError(True, "http_503", "mock overload")
            text = self.cfg.get("transcripts", {}).get(audio.segment_id, self.cfg.get("default_text", ""))
            return TranscriptResult(text, None, (time.perf_counter() - t0) * 1000, {"text": text}, not text.strip())
        finally:
            self._inflight -= 1
