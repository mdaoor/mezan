"""Shared implementation for engines that speak POST {base}/v1/audio/transcriptions."""
from __future__ import annotations

import time

import httpx

from mizan.plugins.base import AdapterError, AudioInput, Capabilities, EngineAdapter, TranscriptResult

NO_TRANSCRIPT_MARKERS = ("", "[IFM-NoTranscript]")


def openai_schema(*, base_url: str, model: str, concurrency: int, api_key_default: str = "EMPTY",
                  api_key_required: bool = False) -> dict:
    props = {
        "base_url": {"type": "string", "format": "uri", "default": base_url},
        "model": {"type": "string", "default": model, "description": "Model id served by the endpoint"},
        "api_key": {"type": "string", "default": api_key_default},
        "language_param": {"type": "boolean", "default": True, "description": "Send the language hint"},
        "extra_form": {"type": "object", "default": {}, "description": "Extra multipart form fields"},
        "max_concurrency": {"type": "integer", "default": concurrency},
        "timeout_s": {"type": "number", "default": 120},
        "hardware": {"type": "string", "default": "", "description": "Free text shown in reports (e.g. 1x H200)"},
    }
    if api_key_required:
        props["api_key"]["minLength"] = 1  # external APIs: an empty default must not satisfy "required"
    req = ["base_url", "model"] + (["api_key"] if api_key_required else [])
    return {"type": "object", "required": req, "properties": props}


class OpenAICompatAdapter(EngineAdapter):
    external = False
    transcriptions_path = "/v1/audio/transcriptions"
    models_path = "/v1/models"
    max_segment_s = 30.0

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            base_url=cfg["base_url"].rstrip("/"),
            headers={"Authorization": f"Bearer {cfg.get('api_key', 'EMPTY')}"},
            timeout=cfg.get("timeout_s", 120),
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(batch=False, streaming=False, language_hint=self.cfg.get("language_param", True),
                            word_timestamps=False, max_segment_s=self.max_segment_s,
                            concurrency_hint=self.cfg.get("max_concurrency", 8), external=self.external)

    async def health(self) -> dict:
        try:
            r = await self.client.get(self.models_path)
            ids = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
            ok = r.status_code == 200 and (not ids or self.cfg["model"] in ids)
            return {"ok": ok, "model": self.cfg["model"], "detail": r.text[:200]}
        except Exception as e:  # network errors are a health answer, not an exception
            return {"ok": False, "model": self.cfg["model"], "detail": f"{type(e).__name__}: {e}"}

    async def list_models(self) -> list[str]:
        r = await self.client.get(self.models_path)
        return [m["id"] for m in r.json().get("data", [])]

    async def transcribe(self, audio: AudioInput, options: dict) -> TranscriptResult:
        form = {"model": self.cfg["model"], "response_format": "json", **self.cfg.get("extra_form", {})}
        lang = audio.language_hint or options.get("language_hint")
        if lang and self.cfg.get("language_param", True):
            form["language"] = lang
        t0 = time.perf_counter()
        try:
            r = await self.client.post(
                self.transcriptions_path,
                files={"file": (f"{audio.segment_id}.wav", audio.pcm16k, "audio/wav")},
                data={k: str(v) if not isinstance(v, str) else v for k, v in form.items()},
                timeout=options.get("timeout_s") or self.cfg.get("timeout_s", 120),
            )
        except httpx.TimeoutException as e:
            raise AdapterError(True, "timeout", str(e)) from e
        except httpx.TransportError as e:
            raise AdapterError(True, "connection", str(e)) from e
        if r.status_code in (408, 429) or r.status_code >= 500:
            raise AdapterError(True, f"http_{r.status_code}", r.text[:200])
        if r.status_code >= 400:
            raise AdapterError(False, f"http_{r.status_code}", r.text[:200])
        try:
            body = r.json()
        except ValueError as e:
            raise AdapterError(False, "bad_json", r.text[:200]) from e
        text = body.get("text", "") if isinstance(body, dict) else str(body)
        return TranscriptResult(
            text=text, spans=None, latency_ms=(time.perf_counter() - t0) * 1000, raw=body,
            no_speech=text.strip() in NO_TRANSCRIPT_MARKERS,
        )

    async def aclose(self) -> None:
        await self.client.aclose()
