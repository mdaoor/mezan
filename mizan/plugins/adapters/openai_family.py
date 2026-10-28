"""Thin adapters over the shared OpenAI-compatible transport."""
from __future__ import annotations

from mizan.plugins.adapters._openai import OpenAICompatAdapter, openai_schema


class VLLMOpenAIAdapter(OpenAICompatAdapter):
    """vLLM serving an ASR model: Jais-ASR, Cohere Transcribe Arabic, Qwen3-ASR, Whisper-on-vLLM."""
    kind = "vllm_openai"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return openai_schema(base_url="http://localhost:8000", model="IFM/Jais-ASR", concurrency=48)

    def capabilities(self):
        c = super().capabilities()
        c.concurrency_hint = self.cfg.get("max_concurrency", 48)
        return c


class HTTPOpenAICompatAdapter(OpenAICompatAdapter):
    """Any server exposing /v1/audio/transcriptions."""
    kind = "http_openai_compat"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return openai_schema(base_url="http://localhost:9000", model="large-v3", concurrency=8)


class FasterWhisperHTTPAdapter(OpenAICompatAdapter):
    """The contrib/faster_whisper_server.py wrapper (OpenAI-compatible)."""
    kind = "faster_whisper_http"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return openai_schema(base_url="http://localhost:9000", model="large-v3", concurrency=8)


class FanarAPIAdapter(OpenAICompatAdapter):
    """Fanar STT hosted API (external). Uses Fanar's OpenAI-style route; verify model id against your account."""
    kind = "fanar_api"
    version = "1.0.0"
    external = True
    transcriptions_path = "/v1/audio/transcriptions"

    @classmethod
    def config_schema(cls) -> dict:
        return openai_schema(base_url="https://api.fanar.qa", model="Fanar-Aura-STT-1", concurrency=8,
                             api_key_default="", api_key_required=True)


class OpenAIAPIAdapter(OpenAICompatAdapter):
    """OpenAI hosted transcription API (external)."""
    kind = "openai_api"
    version = "1.0.0"
    external = True

    @classmethod
    def config_schema(cls) -> dict:
        return openai_schema(base_url="https://api.openai.com", model="whisper-1", concurrency=8,
                             api_key_default="", api_key_required=True)
