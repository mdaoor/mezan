"""Metric set (spec 8.2). All metric code lives here (DoD: no metric computed elsewhere).

Alignment is delegated to jiwer so one alignment engine backs wer/cer/mer and the
script-split variants (so wer_ar + wer_en attribution always adds up to wer).
"""
from __future__ import annotations

import difflib
import math

import jiwer

from mizan.plugins.base import Metric, MetricResult
from mizan.plugins.textutil import script_of

NO_SPEECH_FLAGS = {"no_speech", "music"}


# ---------------------------------------------------------------- alignment
def align_words(ref_tokens: list[str], hyp_tokens: list[str]) -> list[tuple[str, int | None, int | None]]:
    """Token-level ops [(op, ref_idx, hyp_idx)], op in equal|sub|del|ins."""
    if not ref_tokens and not hyp_tokens:
        return []
    if not ref_tokens:
        return [("ins", None, j) for j in range(len(hyp_tokens))]
    if not hyp_tokens:
        return [("del", i, None) for i in range(len(ref_tokens))]
    out = jiwer.process_words(" ".join(ref_tokens), " ".join(hyp_tokens))
    ops: list[tuple[str, int | None, int | None]] = []
    for c in out.alignments[0]:
        if c.type == "equal":
            ops += [("equal", c.ref_start_idx + k, c.hyp_start_idx + k) for k in range(c.ref_end_idx - c.ref_start_idx)]
        elif c.type == "substitute":
            ops += [("sub", c.ref_start_idx + k, c.hyp_start_idx + k) for k in range(c.ref_end_idx - c.ref_start_idx)]
        elif c.type == "delete":
            ops += [("del", c.ref_start_idx + k, None) for k in range(c.ref_end_idx - c.ref_start_idx)]
        elif c.type == "insert":
            ops += [("ins", None, c.hyp_start_idx + k) for k in range(c.hyp_end_idx - c.hyp_start_idx)]
    return ops


def _counts(ops) -> dict:
    return {
        "S": sum(o[0] == "sub" for o in ops),
        "D": sum(o[0] == "del" for o in ops),
        "I": sum(o[0] == "ins" for o in ops),
        "C": sum(o[0] == "equal" for o in ops),
    }


def _ratio(num: float, den: float) -> float:
    return num / den if den else 0.0


def _sum(results: list[MetricResult], key: str) -> float:
    return sum(r.details.get(key, 0) for r in results if r.applicable)


class _ErrorRate(Metric):
    needs_reference = True
    lower_is_better = True

    def aggregate(self, results: list[MetricResult]) -> float:
        errs = _sum(results, "S") + _sum(results, "D") + _sum(results, "I")
        return _ratio(errs, _sum(results, "N"))


class WER(_ErrorRate):
    id = "wer"

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        rt, ht = (ref or "").split(), hyp.split()
        c = _counts(align_words(rt, ht))
        c["N"] = len(rt)
        return MetricResult(_ratio(c["S"] + c["D"] + c["I"], c["N"]) if c["N"] else float(c["I"] > 0), c)


class CER(_ErrorRate):
    id = "cer"

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        r, h = ref or "", hyp
        if not r:
            c = {"S": 0, "D": 0, "I": len(h), "C": 0, "N": 0}
        elif not h:
            c = {"S": 0, "D": len(r), "I": 0, "C": 0, "N": len(r)}
        else:
            o = jiwer.process_characters(r, h)
            c = {"S": o.substitutions, "D": o.deletions, "I": o.insertions, "C": o.hits, "N": len(r)}
        return MetricResult(_ratio(c["S"] + c["D"] + c["I"], c["N"]) if c["N"] else float(c["I"] > 0), c)


class MER(Metric):
    id = "mer"
    needs_reference = True

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        c = _counts(align_words((ref or "").split(), hyp.split()))
        return MetricResult(_ratio(c["S"] + c["D"] + c["I"], c["S"] + c["D"] + c["I"] + c["C"]), c)

    def aggregate(self, results):
        e = _sum(results, "S") + _sum(results, "D") + _sum(results, "I")
        return _ratio(e, e + _sum(results, "C"))


class _ScriptWER(_ErrorRate):
    """WER restricted to one script. Errors are attributed by *reference* token script;
    insertions have no reference token so they are attributed by the inserted hypothesis token's script."""
    script = ""

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        rt, ht = (ref or "").split(), hyp.split()
        c = {"S": 0, "D": 0, "I": 0, "N": sum(script_of(t) == self.script for t in rt)}
        for op, i, j in align_words(rt, ht):
            if op in ("sub", "del") and script_of(rt[i]) == self.script:
                c["S" if op == "sub" else "D"] += 1
            elif op == "ins" and script_of(ht[j]) == self.script:
                c["I"] += 1
        applicable = c["N"] > 0 or c["I"] > 0
        return MetricResult(_ratio(c["S"] + c["D"] + c["I"], c["N"]), c, applicable)


class WERAr(_ScriptWER):
    id = "wer_ar"
    script = "ar"


class WEREn(_ScriptWER):
    id = "wer_en"
    script = "en"


class EnPreservation(Metric):
    id = "en_preservation"
    needs_reference = True
    lower_is_better = False

    def __init__(self, threshold: float = 0.8):
        self.threshold = threshold

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        en_ref = [t for t in (ref or "").split() if script_of(t) == "en"]
        en_hyp = [t for t in hyp.split() if script_of(t) == "en"]
        kept = sum(
            any(difflib.SequenceMatcher(None, r, h).ratio() >= self.threshold for h in en_hyp) for r in en_ref
        )
        return MetricResult(_ratio(kept, len(en_ref)), {"kept": kept, "total": len(en_ref)}, applicable=bool(en_ref))

    def aggregate(self, results):
        return _ratio(_sum(results, "kept"), _sum(results, "total"))


class HallucinationRate(Metric):
    id = "hallucination_rate"
    needs_reference = False

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        applicable = bool(NO_SPEECH_FLAGS & set(segment_flags or []))
        bad = int(applicable and len(hyp.split()) > 2)
        return MetricResult(float(bad), {"bad": bad, "n": int(applicable)}, applicable)

    def aggregate(self, results):
        return _ratio(_sum(results, "bad"), _sum(results, "n"))


class InsertionRate(_ErrorRate):
    id = "insertion_rate"

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        rt = (ref or "").split()
        c = _counts(align_words(rt, hyp.split()))
        c["N"] = len(rt)
        return MetricResult(_ratio(c["I"], c["N"]), c)

    def aggregate(self, results):
        return _ratio(_sum(results, "I"), _sum(results, "N"))


def _pct(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile (numpy 'linear' method) on an already sorted list."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


class Latency(Metric):
    id = "latency_ms"
    needs_reference = False
    unit = "ms"

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        lat = (ctx or {}).get("latency_ms")
        return MetricResult(float(lat or 0.0), {}, applicable=lat is not None)

    def aggregate(self, results):
        v = [r.value for r in results if r.applicable]
        return sum(v) / len(v) if v else 0.0

    def aggregate_all(self, results):
        v = sorted(r.value for r in results if r.applicable)
        return {"latency_ms": self.aggregate(results), "latency_ms_p50": _pct(v, 50), "latency_ms_p95": _pct(v, 95)}


class RTFx(Metric):
    """Σ audio_s / Σ wall_s over per-request latencies (sequential-equivalent RTFx)."""
    id = "rtfx"
    needs_reference = False
    lower_is_better = False

    def score(self, ref, hyp, ref_spans=None, hyp_spans=None, segment_flags=None, ctx=None):
        ctx = ctx or {}
        a, w = float(ctx.get("audio_s") or 0), float(ctx.get("latency_ms") or 0) / 1000
        return MetricResult(_ratio(a, w), {"audio_s": a, "wall_s": w}, applicable=w > 0)

    def aggregate(self, results):
        return _ratio(_sum(results, "audio_s"), _sum(results, "wall_s"))


METRIC_PRESETS = {
    "arabic_codeswitch_default": ["wer", "cer", "mer", "wer_ar", "wer_en", "en_preservation",
                                  "hallucination_rate", "latency_ms"],
    "basic": ["wer", "cer", "mer", "latency_ms"],
    "full": ["wer", "cer", "mer", "wer_ar", "wer_en", "en_preservation", "hallucination_rate",
             "insertion_rate", "latency_ms", "rtfx"],
}


def bootstrap_ci(metric: Metric, results: list[MetricResult], iters: int = 1000, seed: int = 0,
                 alpha: float = 0.05) -> tuple[float, float] | None:
    """Percentile bootstrap CI over segments of the *corpus-level* aggregate (resample segments, re-aggregate).
    Deterministic for a given seed (N4)."""
    import numpy as np

    pool = [r for r in results if r.applicable]
    if len(pool) < 2:
        return None
    rng = np.random.default_rng(seed)
    n = len(pool)
    vals = np.empty(iters)
    for i in range(iters):
        idx = rng.integers(0, n, n)
        vals[i] = metric.aggregate([pool[j] for j in idx])
    lo, hi = np.percentile(vals, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)
