"""Registers every strategy that ships with Mizan."""
from __future__ import annotations

from mizan.plugins import registry as R


def register_all() -> None:
    from mizan.plugins.adapters.aws_transcribe import AWSTranscribeAdapter
    from mizan.plugins.adapters.mock import MockAdapter
    from mizan.plugins.adapters.openai_family import (
        FanarAPIAdapter, FasterWhisperHTTPAdapter, HTTPOpenAICompatAdapter, OpenAIAPIAdapter, VLLMOpenAIAdapter)
    from mizan.plugins.exporters.formats import CSVExporter, HTMLExporter, JSONExporter, JSONLDatasetExporter, MarkdownExporter
    from mizan.plugins.metrics import core as m
    from mizan.plugins.normalizers.arabic_v1 import ArabicV1
    from mizan.plugins.normalizers.simple import NoneNormalizer, WhisperBasic
    from mizan.plugins.segmenters.vad import EnergyVAD, FixedWindow, ManualImport, SileroVAD
    from mizan.plugins.telemetry.sources import DCGMPrometheus, NoTelemetry, NvidiaSMI

    for c in (VLLMOpenAIAdapter, HTTPOpenAICompatAdapter, FasterWhisperHTTPAdapter, FanarAPIAdapter,
              OpenAIAPIAdapter, AWSTranscribeAdapter, MockAdapter):
        R.adapters.register(c)
    for c in (SileroVAD, EnergyVAD, FixedWindow, ManualImport):
        R.segmenters.register(c)
    for c in (ArabicV1, NoneNormalizer, WhisperBasic):
        R.normalizers.register(c)
    for c in (m.WER, m.CER, m.MER, m.WERAr, m.WEREn, m.EnPreservation, m.HallucinationRate, m.InsertionRate,
              m.Latency, m.RTFx):
        R.metrics.register(c)
    for c in (MarkdownExporter, HTMLExporter, JSONExporter, CSVExporter, JSONLDatasetExporter):
        R.exporters.register(c)
    for c in (NvidiaSMI, DCGMPrometheus, NoTelemetry):
        R.telemetry_sources.register(c)
