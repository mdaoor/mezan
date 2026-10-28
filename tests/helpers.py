from __future__ import annotations

import io
import wave

import numpy as np

SR = 16000


def speech_like(duration_s: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration_s * SR)) / SR
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)  # syllable-rate modulation
    x = (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 720 * t)) * env * 0.35
    return x + rng.normal(0, 0.003, len(t))


def make_audio(pattern: list[tuple[str, float]], seed: int = 0) -> np.ndarray:
    """pattern: [("speech", 4.0), ("silence", 1.0), ...] -> float32 samples with a low noise floor."""
    rng = np.random.default_rng(seed)
    parts = []
    for i, (kind, d) in enumerate(pattern):
        parts.append(speech_like(d, seed + i) if kind == "speech" else rng.normal(0, 0.002, int(d * SR)))
    return np.concatenate(parts).astype(np.float32)


def wav_bytes(x: np.ndarray, sr: int = SR) -> bytes:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def utterances(n: int, speech: float = 4.0, gap: float = 1.2, seed: int = 0) -> bytes:
    pat = []
    for _ in range(n):
        pat += [("silence", gap), ("speech", speech)]
    pat.append(("silence", gap))
    return wav_bytes(make_audio(pat, seed))


HIT: set[tuple[str, str]] = set()   # (method, route template) exercised by the suite; checked by test_zz_coverage
_TEMPLATES: dict[int, list] = {}


def route_templates(app) -> list:
    """(template, regex) from the OpenAPI paths, most specific (fewest {params}) first."""
    import re
    key = id(app)
    if key not in _TEMPLATES:
        items = [(t, re.compile(re.sub(r"\{[^}]+\}", "[^/]+", t))) for t in app.openapi()["paths"]]
        _TEMPLATES[key] = sorted(items, key=lambda t: t[0].count("{"))
    return _TEMPLATES[key]
