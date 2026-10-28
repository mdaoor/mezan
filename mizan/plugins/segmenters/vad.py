from __future__ import annotations

import numpy as np

from mizan.plugins.base import Segmenter
from mizan.plugins.segmenters._common import SCHEMA, cfg_with_defaults, finalize, to_samples

SR = 16000


class EnergyVAD(Segmenter):
    """Dependency-free energy VAD (numpy only). Adaptive threshold over the file's own noise floor.
    Always available; ``silero_vad`` is preferred when installed."""
    kind = "energy_vad"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        s = {"type": "object", "properties": dict(SCHEMA["properties"])}
        s["properties"]["margin_db"] = {"type": "number", "default": 10, "description": "dB above the noise floor counted as speech."}
        return s

    def segment(self, pcm16k: bytes, duration_s: float, config: dict) -> list[tuple[float, float]]:
        cfg = cfg_with_defaults(config)
        x = to_samples(pcm16k).astype(np.float32) / 32768.0
        hop = 0.02
        n = int(SR * hop)
        frames = len(x) // n
        if frames == 0:
            return []
        rms = np.sqrt((x[: frames * n].reshape(frames, n) ** 2).mean(axis=1)) + 1e-9
        db = 20 * np.log10(rms)
        floor = np.percentile(db, 10)
        # adaptive over the file's noise floor, but never above -30 dBFS: continuous speech would otherwise
        # make its own level the "floor" and nothing would be detected. Use silero_vad for very noisy audio.
        thr = min(max(floor + float((config or {}).get("margin_db", 10)), -55.0), -30.0)
        speech = db > thr
        # close gaps shorter than min_silence
        gap = max(1, int(cfg["min_silence_ms"] / 1000 / hop))
        spans, start, last = [], None, None
        for i, v in enumerate(speech):
            if v:
                if start is None:
                    start = i
                last = i
            elif start is not None and i - last > gap:
                spans.append((start * hop, (last + 1) * hop))
                start = None
        if start is not None:
            spans.append((start * hop, (last + 1) * hop))
        spans = [(s, e) for s, e in spans if e - s >= 0.2]
        return finalize(spans, duration_s, cfg, energy=(db, hop, thr))


class SileroVAD(Segmenter):
    """Silero VAD via the ``silero-vad`` package (needs torch; install the ``silero`` extra)."""
    kind = "silero_vad"
    version = "1.0.0"
    _model = None

    @classmethod
    def config_schema(cls) -> dict:
        return {"type": "object", "properties": dict(SCHEMA["properties"])}

    def segment(self, pcm16k: bytes, duration_s: float, config: dict) -> list[tuple[float, float]]:
        try:
            import torch
            from silero_vad import get_speech_timestamps, load_silero_vad
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError("silero_vad needs `pip install mizan[silero]`; use kind 'energy_vad' instead") from e
        cfg = cfg_with_defaults(config)
        if SileroVAD._model is None:
            SileroVAD._model = load_silero_vad()
        wav = torch.from_numpy(to_samples(pcm16k).astype("float32") / 32768.0)
        ts = get_speech_timestamps(
            wav, SileroVAD._model, sampling_rate=SR, return_seconds=True,
            min_silence_duration_ms=cfg["min_silence_ms"], speech_pad_ms=0,
            max_speech_duration_s=cfg["max_seg_s"],
        )
        return finalize([(t["start"], t["end"]) for t in ts], duration_s, cfg)


class FixedWindow(Segmenter):
    kind = "fixed_window"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return {"type": "object", "properties": {
            "window_s": {"type": "number", "default": 20, "minimum": 1},
            "overlap_s": {"type": "number", "default": 0, "minimum": 0},
        }}

    def segment(self, pcm16k: bytes, duration_s: float, config: dict) -> list[tuple[float, float]]:
        w, ov = float(config.get("window_s", 20)), float(config.get("overlap_s", 0))
        if ov >= w:
            raise ValueError("overlap_s must be smaller than window_s")
        out, t = [], 0.0
        while t < duration_s - 0.05:
            out.append((round(t, 3), round(min(duration_s, t + w), 3)))
            if t + w >= duration_s:
                break
            t += w - ov
        return out


class ManualImport(Segmenter):
    """Segments supplied by the caller: config = {"segments": [[start, end], ...]}."""
    kind = "manual_import"
    version = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict:
        return {"type": "object", "required": ["segments"], "properties": {
            "segments": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}}
        }}

    def segment(self, pcm16k: bytes, duration_s: float, config: dict) -> list[tuple[float, float]]:
        return [(float(s), min(float(e), duration_s)) for s, e in sorted(config["segments"]) if 0 <= s < e]
