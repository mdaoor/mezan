"""Scoring worker functions (spec 8.1) and corpus-level aggregation."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from mizan.plugins import registry as plugins
from mizan.plugins.base import Metric, MetricResult, Normalizer
from mizan.plugins.textutil import infer_spans

HIDDEN_STRATA = {"priority", "iaa"}
MAX_STRATUM_VALUES = 50


def load_strategies(normalizer_cfg: dict, metric_ids: Iterable[str]) -> tuple[Normalizer, dict[str, Metric]]:
    norm = plugins.normalizers.get(normalizer_cfg.get("kind", "arabic_v1"))(normalizer_cfg.get("config") or {})
    return norm, {mid: plugins.metrics.get(mid)() for mid in metric_ids}


def score_segment(norm: Normalizer, metrics: dict[str, Metric], ref: str | None, ref_spans, hyp: str, hyp_spans,
                  flags: list[str], ctx: dict) -> dict[str, MetricResult]:
    """normalizer(ref), normalizer(hyp) -> every metric in the set. Spans fall back to script inference."""
    ref_n = norm.normalize(ref) if ref is not None else None
    hyp_n = norm.normalize(hyp)
    ref_spans = ref_spans or (infer_spans(ref) if ref else [])
    hyp_spans = hyp_spans or infer_spans(hyp)
    out = {}
    for mid, metric in metrics.items():
        if metric.needs_reference and ref is None:
            continue
        out[mid] = metric.score(ref_n, hyp_n, ref_spans, hyp_spans, flags, ctx)
    return out


def aggregate_run(metrics: dict[str, Metric], rows: list[dict]) -> list[dict]:
    """rows: [{segment_id, strata, scores:{metric_id: MetricResult}}] -> RunSummary dicts.
    Corpus-level (sum errors / sum reference words) overall and per stratum value."""
    groups: dict[tuple[str, str], list[dict]] = {("overall", "all"): rows}
    values: dict[str, set] = defaultdict(set)
    for r in rows:
        for k, v in r["strata"].items():
            if k not in HIDDEN_STRATA and isinstance(v, (str, int, float, bool)):
                values[k].add(str(v))
    for k, vs in values.items():
        if len(vs) > MAX_STRATUM_VALUES:
            continue
        for v in vs:
            groups[(k, v)] = [r for r in rows if str(r["strata"].get(k)) == v]
    out = []
    for (key, val), grp in sorted(groups.items()):
        for mid, metric in metrics.items():
            res = [r["scores"][mid] for r in grp if mid in r["scores"]]
            if not res:
                continue
            n = sum(1 for x in res if x.applicable)
            if n == 0:
                continue
            for out_id, v in metric.aggregate_all(res).items():
                out.append({"stratum_key": key, "stratum_value": val, "metric_id": out_id, "value": float(v), "n": n})
    return out
