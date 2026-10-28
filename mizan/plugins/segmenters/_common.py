from __future__ import annotations

import numpy as np

DEFAULTS = {"max_seg_s": 25.0, "min_seg_s": 3.0, "min_silence_ms": 300, "pad_ms": 250, "force_split_overlap_s": 1.0}

SCHEMA = {
    "type": "object",
    "properties": {
        "max_seg_s": {"type": "number", "default": 25, "minimum": 1, "description": "Hard upper bound per segment."},
        "min_seg_s": {"type": "number", "default": 3, "minimum": 0, "description": "Shorter segments are merged into a neighbour."},
        "min_silence_ms": {"type": "integer", "default": 300, "description": "Silence needed to break a segment."},
        "pad_ms": {"type": "integer", "default": 250, "description": "Padding added around speech."},
        "force_split_overlap_s": {"type": "number", "default": 1.0, "description": "Overlap used when a split must fall inside continuous speech."},
    },
}


def cfg_with_defaults(config: dict | None) -> dict:
    return {**DEFAULTS, **(config or {})}


def to_samples(pcm: bytes) -> np.ndarray:
    """int16 samples from raw PCM16 or a WAV (RIFF) payload."""
    if pcm[:4] == b"RIFF":
        i = pcm.find(b"data")
        pcm = pcm[i + 8:] if i >= 0 else pcm[44:]
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")


def finalize(spans: list[tuple[float, float]], duration_s: float, cfg: dict,
             energy: tuple[np.ndarray, float, float] | None = None) -> list[tuple[float, float]]:
    """pad -> merge short -> split long (rule: silence if available, else overlapped hard cut)."""
    pad = cfg["pad_ms"] / 1000
    padded: list[list[float]] = []
    for s, e in sorted(spans):
        s, e = max(0.0, s - pad), min(duration_s, e + pad)
        if padded and s <= padded[-1][1]:
            padded[-1][1] = max(padded[-1][1], e)
        else:
            padded.append([s, e])

    merged: list[list[float]] = []
    for s, e in padded:
        if merged and ((e - s) < cfg["min_seg_s"] or (merged[-1][1] - merged[-1][0]) < cfg["min_seg_s"]) \
                and s - merged[-1][1] < 2.0 and e - merged[-1][0] <= cfg["max_seg_s"]:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    out: list[tuple[float, float]] = []
    for s, e in merged:
        out.extend(_split(s, e, cfg, energy))
    return [(round(a, 3), round(b, 3)) for a, b in out if b - a >= 0.5]


def _split(s: float, e: float, cfg: dict, energy) -> list[tuple[float, float]]:
    mx, ov = cfg["max_seg_s"], cfg["force_split_overlap_s"]
    res: list[tuple[float, float]] = []
    cur = s
    while e - cur > mx:
        cut, forced = cur + mx, True
        if energy is not None:
            frame_db, hop, speech_thr = energy
            lo, hi = int((cur + 0.6 * mx) / hop), int((cur + mx) / hop)
            win = frame_db[lo:hi]
            if len(win):
                k = int(np.argmin(win))
                cut = (lo + k) * hop
                forced = bool(win[k] > speech_thr)  # even the quietest frame is speech: no real pause to cut at
        res.append((cur, cut))
        cur = cut - ov if forced else cut
    res.append((cur, e))
    return res
