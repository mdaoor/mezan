"""Strategy contracts (spec section 5).

Plain classes only: no framework imports, so every strategy is testable in isolation.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO


class AdapterError(Exception):
    def __init__(self, retryable: bool, error_class: str, message: str = ""):
        super().__init__(message or error_class)
        self.retryable = retryable
        self.error_class = error_class


# ---------------------------------------------------------------- engines
@dataclass
class AudioInput:
    pcm16k: bytes  # 16 kHz mono PCM16 WAV bytes
    duration_s: float
    segment_id: str
    language_hint: str | None = None


@dataclass
class TranscriptResult:
    text: str
    spans: list[dict] | None
    latency_ms: float
    raw: dict
    no_speech: bool = False
    word_timestamps: list[dict] | None = None


@dataclass
class Capabilities:
    batch: bool
    streaming: bool
    language_hint: bool
    word_timestamps: bool
    max_segment_s: float
    concurrency_hint: int
    external: bool

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class EngineAdapter(ABC):
    kind: str = ""
    version: str = "0"

    @classmethod
    @abstractmethod
    def config_schema(cls) -> dict: ...

    @abstractmethod
    def __init__(self, endpoint_config: dict): ...

    @abstractmethod
    async def health(self) -> dict: ...  # {"ok": bool, "model": str, "detail": str}

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    async def transcribe(self, audio: AudioInput, options: dict) -> TranscriptResult: ...

    async def transcribe_batch(self, audios: list[AudioInput], options: dict) -> list[TranscriptResult]:
        return list(await asyncio.gather(*(self.transcribe(a, options) for a in audios)))

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------- segmenter
class Segmenter(ABC):
    kind: str = ""
    version: str = "0"

    @classmethod
    @abstractmethod
    def config_schema(cls) -> dict: ...

    @abstractmethod
    def segment(self, pcm16k: bytes, duration_s: float, config: dict) -> list[tuple[float, float]]: ...


# ------------------------------------------------------------ normalizer
class Normalizer(ABC):
    kind: str = ""
    version: str = "0"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @classmethod
    def config_schema(cls) -> dict:
        return {"type": "object", "properties": {}}

    @abstractmethod
    def normalize(self, text: str) -> str: ...

    @abstractmethod
    def rules(self) -> list[str]: ...


# ---------------------------------------------------------------- metric
@dataclass
class MetricResult:
    value: float
    details: dict = field(default_factory=dict)
    applicable: bool = True  # False => segment does not contribute to the corpus aggregate


class Metric(ABC):
    id: str = ""
    version: str = "1.0.0"
    needs_reference: bool = True
    lower_is_better: bool = True
    unit: str = ""

    @classmethod
    def config_schema(cls) -> dict:
        return {"type": "object", "properties": {}}

    @abstractmethod
    def score(self, ref: str | None, hyp: str, ref_spans, hyp_spans, segment_flags,
              ctx: dict | None = None) -> MetricResult: ...

    @abstractmethod
    def aggregate(self, results: list[MetricResult]) -> float: ...

    def aggregate_all(self, results: list[MetricResult]) -> dict[str, float]:
        """Corpus-level values keyed by metric id (a metric may emit companion ids, e.g. p95)."""
        return {self.id: self.aggregate(results)}


# -------------------------------------------------------------- exporter
@dataclass
class ReportModel:
    title: str
    question: str = ""
    generated_at: datetime | None = None
    sections: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class Exporter(ABC):
    kind: str = ""
    extension: str = ""

    @abstractmethod
    def export(self, report: ReportModel, out: BinaryIO) -> str: ...  # returns mime type


# ------------------------------------------------------------- telemetry
@dataclass
class TelemetrySampleData:
    ts: datetime
    device: str
    util_pct: float | None = None
    mem_used_mb: float | None = None
    temp_c: float | None = None
    power_w: float | None = None


class TelemetrySource(ABC):
    kind: str = ""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    async def sample(self) -> list[TelemetrySampleData]: ...
