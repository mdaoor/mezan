"""Evaluation / agreement / bootstrap runs: creation, control, comparison, disagreements."""
from __future__ import annotations

import itertools
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan import __version__
from mizan.core import models as m
from mizan.core.db import utcnow
from mizan.core.errors import Conflict, Invalid, NotFound
from mizan.core.jobs import enqueue, wake
from mizan.core.services.common import Actor, audit, must_get
from mizan.core.services.registry import engine_kind
from mizan.plugins import registry as plugins
from mizan.plugins.metrics.core import METRIC_PRESETS, align_words

ACTIVE = ("pending", "running", "paused")


def resolve_metric_set(spec) -> list[str]:
    ids = METRIC_PRESETS.get(spec) if isinstance(spec, str) else list(spec or [])
    if ids is None:
        raise Invalid(f"unknown metric preset '{spec}'", presets=sorted(METRIC_PRESETS))
    unknown = [i for i in ids if not plugins.metrics.has(i)]
    if unknown:
        raise Invalid(f"unknown metrics {unknown}", available=plugins.metrics.keys())
    return list(dict.fromkeys(ids))


def run_view(r: m.Run) -> dict:
    return {"id": r.id, "project_id": r.project_id, "type": r.type, "name": r.name, "model_endpoint_id": r.model_endpoint_id,
            "reference": {"type": r.reference_type, "id": r.reference_id}, "dataset_id": r.dataset_id,
            "normalizer": r.normalizer_cfg, "metric_set": r.metric_set, "options": r.options, "status": r.status,
            "progress": {"done": r.progress_done, "total": r.progress_total}, "error": r.error,
            "started_at": r.started_at, "finished_at": r.finished_at, "created_at": r.created_at, "created_by": r.created_by}


async def create_run(c, db: AsyncSession, actor: Actor, req: dict) -> m.Run:
    rtype = req.get("type", "evaluation")
    if rtype not in ("evaluation", "agreement", "bootstrap"):
        raise Invalid("type must be evaluation | agreement | bootstrap")
    ep = await must_get(db, m.ModelEndpoint, req["model_endpoint_id"], "ModelEndpoint")
    kind = await engine_kind(db, ep.engine_id)
    ref = req.get("reference") or {"type": "none"}
    rt, rid = ref.get("type", "none"), ref.get("id")
    dataset_id = req.get("dataset_id")
    ref_label = None
    if rt == "gold_set":
        gs = await must_get(db, m.GoldSet, rid, "GoldSet")
        if dataset_id and dataset_id != gs.dataset_id:
            raise Invalid("dataset_id does not match the gold set's dataset")
        dataset_id, ref_label = gs.dataset_id, {"gold_set": gs.name, "version": gs.version}
    elif rt == "run":
        rr = await must_get(db, m.Run, rid, "Reference run")
        dataset_id = dataset_id or rr.dataset_id
        rtype = "agreement" if rtype == "evaluation" else rtype
        ref_label = {"run": rr.name, "run_id": rr.id}
    elif rt != "none":
        raise Invalid("reference.type must be gold_set | run | none")
    if rtype in ("evaluation", "agreement") and rt == "none":
        raise Invalid(f"{rtype} runs need a reference (gold_set or run)")
    if rtype == "bootstrap" and rt != "none":
        raise Invalid("bootstrap runs are never scored: reference must be none")
    if not dataset_id:
        raise Invalid("dataset_id is required when there is no reference")
    ds = await must_get(db, m.Dataset, dataset_id, "Dataset")

    norm_req = req.get("normalizer") or {"kind": "arabic_v1"}
    norm = plugins.normalizers.get(norm_req["kind"])(norm_req.get("config") or {})
    metric_ids = [] if rtype == "bootstrap" else resolve_metric_set(req.get("metric_set", "arabic_codeswitch_default"))
    opts = req.get("options") or {}
    adapter_cls = plugins.adapters.get(kind)
    caps = adapter_cls({**ep.config, "model": ep.model_id}).capabilities()
    opts = {"concurrency": opts.get("concurrency") or caps.concurrency_hint, "language_hint": opts.get("language_hint"),
            "timeout_s": opts.get("timeout_s", 120), "segment_filter": opts.get("segment_filter") or {},
            "limit": opts.get("limit"), "overwrite_drafts": bool(opts.get("overwrite_drafts", False))}
    seg_cfg = req.get("segmenter") or {}
    secrets = {k for k in ep.config if k in ("api_key", "secret")}
    resolved = {
        "mizan_version": __version__,
        "engine": {"kind": kind, "adapter_version": adapter_cls.version, "external": caps.external},
        "model": {"id": ep.model_id, "endpoint_id": ep.id, "display_name": ep.display_name, "base_url": ep.base_url},
        "endpoint_config": {k: v for k, v in ep.config.items() if k not in secrets},
        "reference": {"type": rt, "id": rid, **(ref_label or {})},
        "dataset": {"id": ds.id, "name": ds.name},
        "segmenter": seg_cfg,
        "normalizer": {"kind": norm.kind, "version": norm.version, "config": norm.config, "rules": norm.rules()},
        "metrics": [{"id": i, "version": plugins.metrics.get(i).version} for i in metric_ids],
        "options": opts, "runtime": {"infer_wall_s": 0.0},
    }
    run = m.Run(project_id=ds.project_id, type=rtype, name=req.get("name") or f"{ep.display_name} · {rtype}",
                model_endpoint_id=ep.id, reference_type=rt, reference_id=rid, dataset_id=dataset_id,
                segmenter_cfg=seg_cfg, normalizer_cfg={"kind": norm.kind, "version": norm.version, "config": norm.config},
                metric_set=metric_ids, options=opts, resolved_config=resolved, created_by=actor.id)
    db.add(run)
    await db.flush()
    await enqueue(c, db, "run", {"run_id": run.id}, actor.id)
    audit(db, actor, "run.create", "run", run.id, {"type": rtype, "endpoint": ep.id})
    return run


async def control(c, db: AsyncSession, actor: Actor, run_id: str, action: str) -> m.Run:
    run = await must_get(db, m.Run, run_id, "Run")
    allowed = {"cancel": ("pending", "running", "paused"), "pause": ("running",), "resume": ("paused",)}
    if action not in allowed:
        raise Invalid("unknown action")
    if run.status not in allowed[action]:
        raise Conflict(f"cannot {action} a {run.status} run")
    run.status = {"cancel": "cancelled", "pause": "paused", "resume": "running"}[action]
    if action == "cancel":
        run.finished_at = utcnow()
    audit(db, actor, f"run.{action}", "run", run_id)
    await db.commit()
    await c.hub.publish(f"run:{run_id}", {"type": "status", "status": run.status})
    return run


async def rerun_missing(c, db: AsyncSession, actor: Actor, run_id: str) -> m.Run:
    """Resume a failed/cancelled run: only segments without a successful hypothesis are recomputed."""
    run = await must_get(db, m.Run, run_id, "Run")
    if run.status in ACTIVE:
        raise Conflict(f"run is {run.status}")
    run.status, run.error, run.finished_at = "pending", None, None
    await enqueue(c, db, "run", {"run_id": run.id}, actor.id)
    await db.commit()
    wake(c)
    return run


# ------------------------------------------------------------------ queries
async def summaries(db: AsyncSession, run_id: str) -> list[dict]:
    rows = (await db.execute(select(m.RunSummary).where(m.RunSummary.run_id == run_id))).scalars()
    return [{"stratum_key": r.stratum_key, "stratum_value": r.stratum_value, "metric_id": r.metric_id, "value": r.value, "n": r.n}
            for r in rows]


def _overall(rows: list[dict]) -> dict:
    return {r["metric_id"]: r["value"] for r in rows if r["stratum_key"] == "overall"}


async def run_detail(db: AsyncSession, run_id: str) -> dict:
    run = await must_get(db, m.Run, run_id, "Run")
    rows = await summaries(db, run_id)
    return {**run_view(run), "summary": _overall(rows), "summary_rows": rows}


async def compare(db: AsyncSession, ids: list[str], stratum: str | None = None) -> dict:
    if len(ids) < 1:
        raise Invalid("ids required")
    runs = [await must_get(db, m.Run, i, "Run") for i in ids]
    allrows = {r.id: await summaries(db, r.id) for r in runs}
    metric_ids = sorted({x["metric_id"] for rows in allrows.values() for x in rows})
    direction = {mid: (plugins.metrics.get(mid).lower_is_better if plugins.metrics.has(mid) else True) for mid in metric_ids}
    direction.update({"latency_ms_p50": True, "latency_ms_p95": True, "rtfx_observed": False, "elapsed_s": True})
    key = stratum.split("=")[0] if stratum else None
    want_val = stratum.split("=", 1)[1] if stratum and "=" in stratum else None

    def table(skey: str, sval: str) -> dict:
        t: dict = {}
        for r in runs:
            t[r.id] = {x["metric_id"]: {"value": x["value"], "n": x["n"]} for x in allrows[r.id]
                       if x["stratum_key"] == skey and x["stratum_value"] == sval}
        best, delta = {}, {}
        for mid in metric_ids:
            vals = {rid: t[rid][mid]["value"] for rid in t if mid in t[rid]}
            if vals:
                best[mid] = (min if direction[mid] else max)(vals, key=vals.get)
                base = vals.get(runs[0].id)
                if base is not None:
                    delta[mid] = {rid: v - base for rid, v in vals.items() if rid != runs[0].id}
        return {"runs": t, "best": best, "delta_vs_baseline": delta}

    out = {"runs": [{"id": r.id, "name": r.name, "type": r.type, "status": r.status, "model_endpoint_id": r.model_endpoint_id,
                     "reference": {"type": r.reference_type, "id": r.reference_id}, "normalizer": r.normalizer_cfg} for r in runs],
           "metrics": metric_ids, "lower_is_better": direction, "baseline": runs[0].id, "overall": table("overall", "all"),
           "labels": {r.id: ("disagreement vs reference run" if r.type == "agreement" else "accuracy vs gold") for r in runs}}
    if key:
        vals = sorted({x["stratum_value"] for rows in allrows.values() for x in rows if x["stratum_key"] == key})
        out["stratum"] = {"key": key, "values": {v: table(key, v) for v in vals if want_val in (None, v)}}
    return out


async def disagreements(db: AsyncSession, ids: list[str], metric: str = "wer", top: int = 100) -> dict:
    """Pairwise normalized edit distance per segment across runs; ranked by the max pair."""
    if len(ids) < 2:
        raise Invalid("need at least two runs")
    if metric not in ("wer", "cer"):
        raise Invalid("metric must be wer or cer")
    runs = [await must_get(db, m.Run, i, "Run") for i in ids]
    norm = plugins.normalizers.get(runs[0].normalizer_cfg.get("kind", "arabic_v1"))(runs[0].normalizer_cfg.get("config") or {})
    hyps: dict[str, dict[str, str]] = defaultdict(dict)
    for r in runs:
        for h in (await db.execute(select(m.Hypothesis).where(m.Hypothesis.run_id == r.id, m.Hypothesis.error.is_(None)))).scalars():
            hyps[h.segment_id][r.id] = h.text
    refs: dict[str, str] = {}
    gold_ids = {r.reference_id for r in runs if r.reference_type == "gold_set"}
    if len(gold_ids) == 1:
        gid = next(iter(gold_ids))
        refs = {i.segment_id: i.text for i in (await db.execute(select(m.GoldItem).where(m.GoldItem.gold_set_id == gid))).scalars()}
    unit = (lambda s: s.split()) if metric == "wer" else (lambda s: list(s.replace(" ", "_")))  # noqa: E731
    ranked = []
    for sid, per in hyps.items():
        if len(per) < len(runs):
            continue
        n = {rid: norm.normalize(t) for rid, t in per.items()}
        best, pair = -1.0, None
        for a, b in itertools.combinations(per, 2):
            ta, tb = unit(n[a]), unit(n[b])
            ops = align_words(ta, tb)
            errs = sum(1 for o in ops if o[0] != "equal")
            d = errs / max(len(ta), len(tb), 1)
            if d > best:
                best, pair = d, (a, b)
        ranked.append((best, sid, pair))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    segs = {s.id: s for s in (await db.execute(select(m.Segment).where(m.Segment.id.in_([t[1] for t in ranked[:top]])))).scalars()}
    items = []
    for d, sid, pair in ranked[:top]:
        s = segs[sid]
        ref_t = refs.get(sid)
        base_txt = norm.normalize(ref_t) if ref_t is not None else norm.normalize(hyps[sid][runs[0].id])
        items.append({
            "segment_id": sid, "audio_file_id": s.audio_file_id, "start_s": s.start_s, "end_s": s.end_s, "distance": d,
            "pair": list(pair), "reference": ref_t,
            "hypotheses": [{"run_id": r.id, "text": hyps[sid][r.id], "diff": _diff(base_txt, norm.normalize(hyps[sid][r.id]))}
                           for r in runs]})
    return {"metric": metric, "compared_runs": [r.id for r in runs], "diff_base": "reference" if refs else "first run",
            "normalizer": runs[0].normalizer_cfg, "items": items, "total_segments": len(ranked)}


def _diff(ref: str, hyp: str) -> list[dict]:
    rt, ht = ref.split(), hyp.split()
    return [{"op": op, "ref": rt[i] if i is not None else None, "hyp": ht[j] if j is not None else None}
            for op, i, j in align_words(rt, ht)]


async def hypotheses_page(db: AsyncSession, run_id: str, limit: int, offset: int, search: str | None = None,
                          errors_only: bool = False) -> dict:
    q = (select(m.Hypothesis, m.Segment).join(m.Segment, m.Segment.id == m.Hypothesis.segment_id)
         .where(m.Hypothesis.run_id == run_id).order_by(m.Segment.audio_file_id, m.Segment.idx))
    if search:
        q = q.where(m.Hypothesis.text.contains(search))
    if errors_only:
        q = q.where(m.Hypothesis.error.is_not(None))
    rows = (await db.execute(q.offset(offset).limit(limit + 1))).all()
    items = [{"id": h.id, "segment_id": h.segment_id, "audio_file_id": s.audio_file_id, "start_s": s.start_s, "end_s": s.end_s,
              "text": h.text, "spans": h.spans, "latency_ms": h.latency_ms, "error": h.error, "attempt": h.attempt}
             for h, s in rows[:limit]]
    score_rows = []
    if items:
        score_rows = (await db.execute(select(m.SegmentScore).where(
            m.SegmentScore.run_id == run_id, m.SegmentScore.segment_id.in_([i["segment_id"] for i in items])))).scalars().all()
    by_seg: dict[str, dict] = defaultdict(dict)
    for sc in score_rows:
        if sc.applicable:
            by_seg[sc.segment_id][sc.metric_id] = sc.value
    for i in items:
        i["scores"] = by_seg.get(i["segment_id"], {})
    return {"items": items, "next_cursor": str(offset + limit) if len(rows) > limit else None}


async def scores_query(db: AsyncSession, run_id: str, stratum: str | None, metric: str | None) -> dict:
    rows = await summaries(db, run_id)
    if metric:
        rows = [r for r in rows if r["metric_id"] == metric]
    if stratum:
        k = stratum.split("=")[0]
        rows = [r for r in rows if r["stratum_key"] == k and (("=" not in stratum) or r["stratum_value"] == stratum.split("=", 1)[1])]
    return {"run_id": run_id, "rows": rows}


async def segment_scores_page(db: AsyncSession, run_id: str, metric: str, limit: int, offset: int, worst_first: bool = True) -> dict:
    q = select(m.SegmentScore).where(m.SegmentScore.run_id == run_id, m.SegmentScore.metric_id == metric, m.SegmentScore.applicable.is_(True))
    q = q.order_by(m.SegmentScore.value.desc() if worst_first else m.SegmentScore.value).offset(offset).limit(limit)
    return {"items": [{"segment_id": s.segment_id, "value": s.value, "details": s.details}
                      for s in (await db.execute(q)).scalars()]}
