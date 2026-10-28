"""AWS Transcribe (external, async job). boto3 is imported lazily; pass ``client_factory`` in tests."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
import uuid

from mizan.plugins.base import AdapterError, AudioInput, Capabilities, EngineAdapter, TranscriptResult


class AWSTranscribeAdapter(EngineAdapter):
    kind = "aws_transcribe"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return {"type": "object", "required": ["bucket", "region", "model"], "properties": {
            "bucket": {"type": "string", "description": "S3 bucket used for staging audio"},
            "region": {"type": "string", "default": "us-east-1"},
            "model": {"type": "string", "default": "aws-transcribe", "description": "Label only"},
            "base_url": {"type": "string", "default": "aws://transcribe"},
            "language_code": {"type": "string", "default": "ar-AE"},
            "poll_s": {"type": "number", "default": 2},
            "max_concurrency": {"type": "integer", "default": 8},
            "timeout_s": {"type": "number", "default": 300},
        }}

    def __init__(self, cfg: dict, client_factory=None):
        self.cfg = cfg
        self._factory = client_factory
        self._clients = None

    def _c(self):
        if self._clients is None:
            if self._factory:
                self._clients = self._factory()
            else:  # pragma: no cover - needs boto3 + credentials
                import boto3
                r = self.cfg["region"]
                self._clients = (boto3.client("s3", region_name=r), boto3.client("transcribe", region_name=r))
        return self._clients

    def capabilities(self) -> Capabilities:
        return Capabilities(batch=False, streaming=False, language_hint=True, word_timestamps=False,
                            max_segment_s=300, concurrency_hint=self.cfg.get("max_concurrency", 8), external=True)

    async def health(self) -> dict:
        try:
            await asyncio.to_thread(self._c()[1].list_transcription_jobs, MaxResults=1)
            return {"ok": True, "model": self.cfg["model"], "detail": "transcribe reachable"}
        except Exception as e:
            return {"ok": False, "model": self.cfg["model"], "detail": f"{type(e).__name__}: {e}"}

    async def transcribe(self, audio: AudioInput, options: dict) -> TranscriptResult:
        s3, tr = self._c()
        job, key = f"mizan-{uuid.uuid4().hex}", f"mizan/{uuid.uuid4().hex}.wav"
        t0 = time.perf_counter()
        deadline = t0 + options.get("timeout_s", self.cfg.get("timeout_s", 300))
        try:
            await asyncio.to_thread(s3.put_object, Bucket=self.cfg["bucket"], Key=key, Body=audio.pcm16k)
            await asyncio.to_thread(
                tr.start_transcription_job, TranscriptionJobName=job, MediaFormat="wav",
                Media={"MediaFileUri": f"s3://{self.cfg['bucket']}/{key}"},
                LanguageCode={"ar": "ar-AE", "en": "en-US"}.get(audio.language_hint or "", self.cfg.get("language_code", "ar-AE")),
            )
            while True:
                j = (await asyncio.to_thread(tr.get_transcription_job, TranscriptionJobName=job))["TranscriptionJob"]
                if j["TranscriptionJobStatus"] == "COMPLETED":
                    uri = j["Transcript"]["TranscriptFileUri"]
                    body = json.loads(await asyncio.to_thread(lambda: urllib.request.urlopen(uri).read()))
                    text = body["results"]["transcripts"][0]["transcript"]
                    return TranscriptResult(text, None, (time.perf_counter() - t0) * 1000, body, not text.strip())
                if j["TranscriptionJobStatus"] == "FAILED":
                    raise AdapterError(False, "job_failed", j.get("FailureReason", ""))
                if time.perf_counter() > deadline:
                    raise AdapterError(True, "timeout")
                await asyncio.sleep(self.cfg.get("poll_s", 2))
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(True, type(e).__name__, str(e)) from e
        finally:
            try:
                await asyncio.to_thread(s3.delete_object, Bucket=self.cfg["bucket"], Key=key)
            except Exception:
                pass
