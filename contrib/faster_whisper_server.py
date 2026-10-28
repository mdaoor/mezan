"""OpenAI-compatible wrapper around faster-whisper (POST /v1/audio/transcriptions, GET /v1/models).

    pip install faster-whisper fastapi uvicorn python-multipart
    WHISPER_MODEL=large-v3 python contrib/faster_whisper_server.py   # :9000
"""
import asyncio
import os
import tempfile

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile

MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")
app = FastAPI(title="faster-whisper (OpenAI-compatible)")
_model = None
_gate = asyncio.Semaphore(int(os.environ.get("WHISPER_CONCURRENCY", "4")))


def model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_NAME, device=os.environ.get("WHISPER_DEVICE", "cuda"),
                              compute_type=os.environ.get("WHISPER_COMPUTE", "float16"))
    return _model


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]}


@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL_NAME}


def _run(path: str, language: str | None):
    segs, _ = model().transcribe(path, language=language or None, vad_filter=False, beam_size=5)
    return " ".join(s.text.strip() for s in segs).strip()


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model_: str = Form("", alias="model"), language: str | None = Form(None),
                     response_format: str = Form("json")):
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(await file.read())
        f.flush()
        async with _gate:
            text = await asyncio.to_thread(_run, f.name, language)
    return {"text": text} if response_format == "json" else text


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9000")))
