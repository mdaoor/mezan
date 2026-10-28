"""Campaign reports — template ``decision_v1`` (spec 8.5). The report is stored as sections -> blocks JSON;
exporters (markdown/html/json/csv) render that structure, so a new format needs no change here."""
from __future__ import annotations

import hashlib
import json
import zlib
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.db import utcnow
from mizan.core.errors import Conflict, Invalid, NotFound
from mizan.core.jobs import enqueue
from mizan.core.services import loadtest, runs as runsvc
from mizan.core.services.common import Actor, audit, must_get
from mizan.plugins import registry as plugins
from mizan.plugins.base import MetricResult, ReportModel
from mizan.plugins.metrics.core import bootstrap_ci

DEFAULT_WEIGHTS = {"accuracy": 0.5, "code_switch": 0.2, "throughput": 0.2, "robustness": 0.1}
COMPONENT_LABEL = {"accuracy": "Accuracy", "code_switch": "Code-switch", "throughput": "Throughput", "robustness": "Robustness"}
TEMPLATES = ("decision_v1",)


# --------------------------------------------------------------------- campaigns
async def create_campaign(db: AsyncSession, actor: Actor, project_id: str, name: str, question: str,
                          run_ids: list[str], load_run_ids: list[str]) -> m.Campaign:
    await must_get(db, m.Project, project_id, "Project")
    cp = m.Campaign(project_id=project_id, name=name, question=question, created_by=actor.id)
    db.add(cp)
    await db.flush()
    await attach(db, cp, run_ids, load_run_ids)
    audit(db, actor, "campaign.create", "campaign", cp.id, {"name": name})
    return cp


async def attach(db: AsyncSession, cp: m.Campaign, run_ids: list[str], load_run_ids: list[str]) -> None:
    have_r = set((await db.execute(select(m.CampaignRun.run_id).where(m.CampaignRun.campaign_id == cp.id))).scalars())
    have_l = set((await db.execute(select(m.CampaignLoadRun.load_run_id).where(m.CampaignLoadRun.campaign_id == cp.id))).scalars())
    for rid in run_ids:
        r = await must_get(db, m.Run, rid, "Run")
        if r.type == "bootstrap":
            raise Invalid("bootstrap runs are never scored and cannot be attached to a campaign")
        if rid not in have_r:
            db.add(m.CampaignRun(campaign_id=cp.id, run_id=rid))
    for lid in load_run_ids:
        await must_get(db, m.LoadRun, lid, "LoadRun")
        if lid not in have_l:
            db.add(m.CampaignLoadRun(campaign_id=cp.id, load_run_id=lid))


async def campaign_view(db: AsyncSession, cp: m.Campaign) -> dict:
    rids = (await db.execute(select(m.CampaignRun.run_id).where(m.CampaignRun.campaign_id == cp.id))).scalars().all()
    lids = (await db.execute(select(m.CampaignLoadRun.load_run_id).where(m.CampaignLoadRun.campaign_id == cp.id))).scalars().all()
    return {"id": cp.id, "project_id": cp.project_id, "name": cp.name, "question": cp.question, "status": cp.status,
            "run_ids": list(rids), "load_run_ids": list(lids), "created_at": cp.created_at}


async def request_report(c, db: AsyncSession, actor: Actor, cp: m.Campaign, template: str, options: dict) -> m.Report:
    if template not in TEMPLATES:
        raise Invalid(f"unknown template '{template}'", available=TEMPLATES)
    ver = ((await db.execute(select(func.max(m.Report.version)).where(m.Report.campaign_id == cp.id))).scalar() or 0) + 1
    rep = m.Report(campaign_id=cp.id, version=ver, template=template, options=options or {}, created_by=actor.id)
    db.add(rep)
    await db.flush()
    await enqueue(c, db, "report", {"report_id": rep.id}, actor.id)
    return rep


# ---------------------------------------------------------------------- collect
def config_hash(resolved: dict) -> str:
    core = {k: v for k, v in resolved.items() if k not in ("runtime", "preflight")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:10]


async def collect(db: AsyncSession, cp: m.Campaign, options: dict) -> dict:
    rids = (await db.execute(select(m.CampaignRun.run_id).where(m.CampaignRun.campaign_id == cp.id))).scalars().all()
    lids = (await db.execute(select(m.CampaignLoadRun.load_run_id).where(m.CampaignLoadRun.campaign_id == cp.id))).scalars().all()
    runs = [r for r in (await db.execute(select(m.Run).where(m.Run.id.in_(rids)).order_by(m.Run.created_at))).scalars()
            if r.status == "succeeded"] if rids else []
    loads = [x for x in (await db.execute(select(m.LoadRun).where(m.LoadRun.id.in_(lids)).order_by(m.LoadRun.created_at))).scalars()
             if x.status == "succeeded"] if lids else []
    skipped = [r.id for r in (await db.execute(select(m.Run).where(m.Run.id.in_(rids)))).scalars() if r.status != "succeeded"] if rids else []
    ep_ids = {r.model_endpoint_id for r in runs} | {x.model_endpoint_id for x in loads}
    endpoints = {}
    for ep in (await db.execute(select(m.ModelEndpoint).where(m.ModelEndpoint.id.in_(ep_ids)))).scalars() if ep_ids else []:
        eng = await db.get(m.Engine, ep.engine_id)
        endpoints[ep.id] = {"id": ep.id, "name": ep.display_name, "engine_kind": eng.kind, "model_id": ep.model_id,
                            "external": eng.external, "hardware": ep.config.get("hardware", ""), "base_url": ep.base_url}
    run_data = []
    for r in runs:
        rows = await runsvc.summaries(db, r.id)
        ref_label = r.resolved_config.get("reference", {})
        run_data.append({"id": r.id, "name": r.name, "type": r.type, "endpoint_id": r.model_endpoint_id, "reference": ref_label,
                         "reference_type": r.reference_type, "reference_id": r.reference_id, "dataset_id": r.dataset_id,
                         "rows": rows, "overall": {x["metric_id"]: x["value"] for x in rows if x["stratum_key"] == "overall"},
                         "n": next((x["n"] for x in rows if x["stratum_key"] == "overall" and x["metric_id"] == "wer"), 0),
                         "resolved": r.resolved_config, "cfg_hash": config_hash(r.resolved_config),
                         "normalizer": r.normalizer_cfg, "metric_set": r.metric_set, "options": r.options})
    # bootstrap CI (1000 resamples over segments)
    ci = {}
    iters = int(options.get("bootstrap_iters", 1000))
    for rd in run_data:
        ci[rd["id"]] = {}
        for mid in ("wer", "cer"):
            if mid not in rd["metric_set"]:
                continue
            scs = (await db.execute(select(m.SegmentScore).where(m.SegmentScore.run_id == rd["id"], m.SegmentScore.metric_id == mid))).scalars().all()
            res = [MetricResult(s.value, s.details, s.applicable) for s in scs]
            seed = zlib.crc32(f"{rd['id']}:{mid}".encode())
            ci[rd["id"]][mid] = bootstrap_ci(plugins.metrics.get(mid)(), res, iters, seed)
    # reference context (gold)
    gold = None
    gids = {rd["reference_id"] for rd in run_data if rd["reference_type"] == "gold_set"}
    if gids:
        from mizan.core.services import gold as goldsvc
        gs = await db.get(m.GoldSet, sorted(gids)[0])
        gold = await goldsvc.stats(db, gs)
        gold["truncated"] = sum("truncated" in (i.flags or []) for i in (await db.execute(
            select(m.GoldItem).where(m.GoldItem.gold_set_id == gs.id))).scalars())
        gold["extra_gold_sets"] = len(gids) - 1
    load_data = []
    for x in loads:
        load_data.append(await loadtest.detail(db, x.id))
    data = {"campaign": {"id": cp.id, "name": cp.name, "question": cp.question}, "endpoints": endpoints, "runs": run_data,
            "ci": ci, "loads": load_data, "gold": gold, "skipped_runs": skipped, "samples": [], "disagreements": []}
    # primary eval run per endpoint = latest
    prim = {}
    for rd in run_data:
        prim[rd["endpoint_id"]] = rd
    # samples + disagreements need cross-run joins
    prim_runs = list(prim.values())
    if len(prim_runs) >= 1:
        data["samples"] = await _code_switch_samples(db, prim_runs, int(options.get("sample_count", 10)))
    if len(prim_runs) >= 2:
        try:
            data["disagreements"] = (await runsvc.disagreements(db, [r["id"] for r in prim_runs], "wer", int(options.get("disagreement_top", 20))))["items"]
        except Invalid:
            pass
    return data


async def _code_switch_samples(db: AsyncSession, prim_runs: list[dict], n: int) -> list[dict]:
    ids = [r["id"] for r in prim_runs]
    hyps: dict[str, dict[str, str]] = {}
    for h in (await db.execute(select(m.Hypothesis).where(m.Hypothesis.run_id.in_(ids), m.Hypothesis.error.is_(None)))).scalars():
        hyps.setdefault(h.segment_id, {})[h.run_id] = h.text
    full = {sid: per for sid, per in hyps.items() if len(per) == len(ids)}
    if not full:
        return []
    # prefer mixed-language reference segments, ranked by mean segment WER (worst first)
    wer = {}
    for s in (await db.execute(select(m.SegmentScore).where(m.SegmentScore.run_id.in_(ids), m.SegmentScore.metric_id == "wer"))).scalars():
        wer.setdefault(s.segment_id, []).append(s.value)
    gold_ids = {r["reference_id"] for r in prim_runs if r["reference_type"] == "gold_set"}
    ref, mixed = {}, set()
    if len(gold_ids) == 1:
        for it in (await db.execute(select(m.GoldItem).where(m.GoldItem.gold_set_id == next(iter(gold_ids)), m.GoldItem.segment_id.in_(full)))).scalars():
            ref[it.segment_id] = it.text
            if it.strata.get("lang_mix") == "mixed":
                mixed.add(it.segment_id)
    order = sorted(full, key=lambda s: (s not in mixed, -(sum(wer.get(s, [0])) / max(len(wer.get(s, [0])), 1)), s))
    return [{"segment_id": s, "reference": ref.get(s), "hyps": full[s], "mixed": s in mixed} for s in order[:n]]


# ---------------------------------------------------------------------- decision
def sustainable_rtfx(load: dict | None) -> float | None:
    if not load or not load["steps"]:
        return None
    ok = [s["rtfx"] for s in load["steps"] if s["error_rate"] <= 0.01]
    return max(ok) if ok else 0.0


def decision_components(data: dict) -> dict:
    """Per-endpoint component scores in [0,1] (None when the evidence is missing)."""
    prim, loads = {}, {}
    for rd in data["runs"]:
        prim[rd["endpoint_id"]] = rd
    for ld in data["loads"]:
        loads[ld["model_endpoint_id"]] = ld
    rtfx = {ep: sustainable_rtfx(loads.get(ep)) for ep in data["endpoints"]}
    top = max([v for v in rtfx.values() if v] or [0])
    comps: dict[str, dict] = {}
    for ep in data["endpoints"]:
        rd, o = prim.get(ep), (prim[ep]["overall"] if ep in prim else {})
        acc = cs = rob = None
        if rd and rd["type"] == "evaluation":  # agreement-mode results are never called accuracy
            if "wer" in o:
                acc = max(0.0, 1 - o["wer"])
            parts = [max(0.0, 1 - o[k]) for k in ("wer_ar", "wer_en") if k in o] + ([o["en_preservation"]] if "en_preservation" in o else [])
            cs = sum(parts) / len(parts) if parts else None
        if rd:
            rp = [1 - min(1.0, o[k]) for k in ("hallucination_rate", "insertion_rate") if k in o]
            tot = o.get("segments_ok", 0) + o.get("segments_failed", 0)
            if tot:
                rp.append(1 - o.get("segments_failed", 0) / tot)
            rob = sum(rp) / len(rp) if rp else None
        thr = (rtfx[ep] / top) if (rtfx.get(ep) is not None and top) else (0.0 if rtfx.get(ep) == 0 else None)
        comps[ep] = {"accuracy": acc, "code_switch": cs, "throughput": thr, "robustness": rob, "rtfx": rtfx.get(ep)}
    return comps


def rank(components: dict, weights: dict) -> list[dict]:
    out = []
    for ep, c in components.items():
        avail = {k: w for k, w in weights.items() if c.get(k) is not None and w > 0}
        tw = sum(avail.values())
        score = sum(c[k] * w for k, w in avail.items()) / tw if tw else None
        out.append({"endpoint_id": ep, "score": score, "missing": [k for k in weights if c.get(k) is None],
                    "weights_used": {k: w / tw for k, w in avail.items()} if tw else {}})
    out.sort(key=lambda r: (-1 if r["score"] is None else -r["score"], r["endpoint_id"]))
    return out


def normalize_weights(w: dict | None) -> dict:
    w = {k: float(v) for k, v in (w or DEFAULT_WEIGHTS).items() if k in DEFAULT_WEIGHTS}
    for k in DEFAULT_WEIGHTS:
        w.setdefault(k, 0.0)
    if any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
        raise Invalid("weights must be non-negative and not all zero")
    s = sum(w.values())
    return {k: v / s for k, v in w.items()}


def endpoint_facts(data: dict) -> dict:
    prim = {rd["endpoint_id"]: rd for rd in data["runs"]}
    load = {ld["model_endpoint_id"]: ld for ld in data["loads"]}
    facts = {}
    for ep, e in data["endpoints"].items():
        rd, ld = prim.get(ep), load.get(ep)
        facts[ep] = {"name": e["name"], "external": e["external"], "run_type": rd["type"] if rd else None,
                     "wer": rd["overall"].get("wer") if rd else None, "cer": rd["overall"].get("cer") if rd else None,
                     "rtfx": sustainable_rtfx(ld), "saturation": ld.get("saturation") if ld else None}
    return facts


def draft_summary(facts: dict, ranking: list[dict]) -> str:
    scored = [r for r in ranking if r["score"] is not None]
    if not scored:
        return "There is not yet enough evidence to recommend an endpoint: attach succeeded evaluation and load runs."
    best = scored[0]
    f = facts[best["endpoint_id"]]
    parts = [f"Recommended endpoint: {f['name']} (decision score {best['score']:.2f})."]
    if f["wer"] is not None:
        label = "WER" if f["run_type"] == "evaluation" else "disagreement WER"
        parts.append(f"It reaches {label} {f['wer']:.1%}" + (f" and CER {f['cer']:.1%}" if f["cer"] is not None else "") + ".")
        if len(scored) > 1 and facts[scored[1]["endpoint_id"]]["wer"] is not None:
            nxt = facts[scored[1]["endpoint_id"]]
            parts.append(f"The runner-up, {nxt['name']}, scores {nxt['wer']:.1%}.")
    if f["rtfx"] is not None:
        sat = f["saturation"]
        parts.append(f"Sustainable throughput is {f['rtfx']:.1f}x real time"
                     + (f"; saturation is reached at concurrency {sat['concurrency']} ({sat['reason'].replace('_', ' ')})." if sat
                        else "; no saturation was reached in the tested range."))
    if best["missing"]:
        parts.append("Caveat: no evidence for " + ", ".join(COMPONENT_LABEL[k].lower() for k in best["missing"])
                     + "; remaining weights were renormalized.")
    if f["external"]:
        parts.append("Note: this endpoint is external, audio and transcripts leave the host.")
    return " ".join(parts)


# ------------------------------------------------------------------------ build
def _t(columns, rows, title=None) -> dict:
    return {"type": "table", "title": title, "columns": columns, "rows": rows}


def _p(text) -> dict:
    return {"type": "paragraph", "text": text}


def build_report(data: dict, options: dict | None = None, now: datetime | None = None) -> dict:
    options = options or {}
    weights = normalize_weights(options.get("weights"))
    eps = data["endpoints"]
    name = lambda ep: eps[ep]["name"]  # noqa: E731
    prim = {}
    for rd in data["runs"]:
        prim[rd["endpoint_id"]] = rd
    loads = {ld["model_endpoint_id"]: ld for ld in data["loads"]}
    gold = data["gold"]
    comps = decision_components(data)
    ranking = rank(comps, weights)
    sections: list[dict] = []

    # 1 ---- recommendation
    caveats = []
    if data["skipped_runs"]:
        caveats.append(f"{len(data['skipped_runs'])} attached run(s) did not succeed and are excluded.")
    if any(r["type"] == "agreement" for r in data["runs"]):
        caveats.append("Agreement-mode runs are reported as disagreement vs their reference run, never as accuracy.")
    if len({r['reference_id'] for r in data["runs"]}) > 1:
        caveats.append("Attached runs use different references; compare them with care.")
    facts = endpoint_facts(data)
    summary_text = options.get("summary_text") or draft_summary(facts, ranking)
    sections.append({"id": "summary", "title": "Recommendation", "blocks": [_p(summary_text)] + (
        [{"type": "bullets", "items": caveats}] if caveats else [])})

    # 2 ---- setup
    setup_rows = []
    for ep, e in eps.items():
        rd = prim.get(ep)
        gpus = sorted({d for ld in ([loads[ep]] if ep in loads else []) for s in ld["steps"] for d in (s.get("gpu") or {})})
        hw = e["hardware"] or (", ".join(gpus) if gpus else "—")
        setup_rows.append([e["name"] + (" ⚠ external" if e["external"] else ""), e["engine_kind"], e["model_id"], hw,
                           rd["cfg_hash"] if rd else "—"])
    blocks = [_t(["Endpoint", "Engine", "Model", "Hardware", "Config hash"], setup_rows)]
    if gold:
        mix = gold["strata_distribution"].get("lang_mix", {})
        blocks.append(_p(f"Gold set: {gold['name']} v{gold['version']} — {gold['segment_count']} segments, {gold['minutes']:.1f} minutes of audio"
                         + (f" (language mix: {', '.join(f'{k} {v}' for k, v in sorted(mix.items()))})" if mix else "") + "."))
    norms = {(r["normalizer"].get("kind"), r["normalizer"].get("version")): r["resolved"].get("normalizer", {}).get("rules", []) for r in data["runs"]}
    for (k, v), rules in norms.items():
        blocks += [_p(f"Normalizer: {k} v{v} — rules applied to both reference and hypothesis:"), {"type": "bullets", "items": rules}]
    sections.append({"id": "setup", "title": "Setup", "blocks": blocks})

    # 3 ---- functional
    fb: list[dict] = []
    ev = [prim[e] for e in eps if e in prim]
    if ev:
        cols = ["Endpoint", "WER", "WER 95% CI", "CER", "MER", "WER-AR", "WER-EN", "EN-preservation", "Halluc.", "p95 latency (ms)"]
        rows = []
        for rd in ev:
            o, ci = rd["overall"], data["ci"].get(rd["id"], {}).get("wer")
            rows.append([name(rd["endpoint_id"]) + ("" if rd["type"] == "evaluation" else " (agreement)"), o.get("wer"),
                         f"{ci[0]:.3f}–{ci[1]:.3f}" if ci else "—", o.get("cer"), o.get("mer"), o.get("wer_ar"), o.get("wer_en"),
                         o.get("en_preservation"), o.get("hallucination_rate"), o.get("latency_ms_p95")])
        agree = any(r["type"] == "agreement" for r in ev)
        fb.append(_t(cols, rows, "Overall (corpus-level; lower is better except EN-preservation)" if not agree else
                     "Overall — agreement-mode rows are disagreement vs the reference run, not accuracy"))
        fb.append({"type": "chart", "kind": "bar", "title": "WER by endpoint", "x_label": "endpoint", "y_label": "WER",
                   "categories": [name(r["endpoint_id"]) for r in ev], "series": [{"name": "WER", "values": [r["overall"].get("wer") for r in ev]}]})
        fb.append(_p(f"Confidence: 95% percentile-bootstrap intervals over segments (1,000 resamples, seeded). "
                     f"Scored on {max((r['n'] for r in ev), default=0)} segments per endpoint."))
        keys = []
        for rd in ev:
            for x in rd["rows"]:
                if x["stratum_key"] != "overall" and x["stratum_key"] not in keys:
                    keys.append(x["stratum_key"])
        keys.sort(key=lambda k: (k not in ("lang_mix", "duration_bucket"), k))
        for k in keys:
            vals = sorted({x["stratum_value"] for rd in ev for x in rd["rows"] if x["stratum_key"] == k})
            rows = []
            for v in vals:
                for rd in ev:
                    g = {x["metric_id"]: x for x in rd["rows"] if x["stratum_key"] == k and x["stratum_value"] == v}
                    if "wer" in g:
                        rows.append([v, name(rd["endpoint_id"]), g["wer"]["n"], g["wer"]["value"], g.get("cer", {}).get("value"),
                                     g.get("mer", {}).get("value")])
            if rows and k in ("lang_mix", "duration_bucket") or (rows and options.get("all_strata")):
                fb.append({"type": "heading", "text": f"By {k.replace('_', ' ')}"})
                fb.append(_t([k, "Endpoint", "Segments", "WER", "CER", "MER"], rows))
    else:
        fb.append(_p("No succeeded evaluation runs are attached."))
    sections.append({"id": "functional", "title": "Functional results", "blocks": fb})

    # 4 ---- code switching
    cb = []
    if ev:
        cb.append(_t(["Endpoint", "WER-AR", "WER-EN", "EN-preservation"],
                     [[name(r["endpoint_id"]), r["overall"].get("wer_ar"), r["overall"].get("wer_en"), r["overall"].get("en_preservation")] for r in ev]))
        if data["samples"]:
            hdr = ["#", "Reference"] + [name(r["endpoint_id"]) for r in ev]
            cb.append(_t(hdr, [[i + 1, s["reference"] if s["reference"] is not None else "—"] + [s["hyps"].get(r["id"], "—") for r in ev]
                               for i, s in enumerate(data["samples"])],
                         "Sample segments (mixed-language references first, hardest first)"))
    sections.append({"id": "code_switching", "title": "Code-switching focus", "blocks": cb or [_p("No data.")]})

    # 5 ---- robustness
    rb = []
    if ev:
        rows = []
        for r in ev:
            o = r["overall"]
            rows.append([name(r["endpoint_id"]), o.get("hallucination_rate"), o.get("insertion_rate"), int(o.get("segments_failed", 0)),
                         gold["truncated"] if gold else "—"])
        rb.append(_t(["Endpoint", "Hallucination rate", "Insertion rate", "Failed segments", "Truncated segments in reference"], rows))
        rb.append(_p("Hallucination rate: share of no-speech/music segments where the model emitted more than two tokens. "
                     "Insertion rate: insertions / reference words (catches repetition loops)."))
    sections.append({"id": "robustness", "title": "Robustness", "blocks": rb or [_p("No data.")]})

    # 6 ---- non-functional
    nb = []
    ld_list = [(ep, loads[ep]) for ep in eps if ep in loads]
    if ld_list:
        for metric, title, ylab in (("p95", "p95 latency vs concurrency", "ms"), ("rtfx", "RTFx vs concurrency", "x real time")):
            nb.append({"type": "chart", "kind": "line", "title": title, "x_label": "concurrency", "y_label": ylab,
                       "series": [{"name": name(ep), "points": [[s["concurrency"], s[metric]] for s in ld["steps"]]} for ep, ld in ld_list]})
        rows = []
        for ep, ld in ld_list:
            for s in ld["steps"]:
                gpu = s.get("gpu") or {}
                util = max([g.get("util_max") for g in gpu.values() if g.get("util_max") is not None], default=None)
                vram = max([g.get("mem_used_mb_max") for g in gpu.values() if g.get("mem_used_mb_max") is not None], default=None)
                rows.append([name(ep), s["concurrency"], s["p50"], s["p95"], s["p99"], s["rtfx"], s["req_per_s"],
                             s["error_rate"] * 100, util, vram])
        nb.append(_t(["Endpoint", "Concurrency", "p50 (ms)", "p95 (ms)", "p99 (ms)", "RTFx", "req/s", "Error %", "GPU util % (max)", "VRAM MB (max)"], rows))
        sat_lines = []
        for ep, ld in ld_list:
            s = ld.get("saturation")
            sat_lines.append(f"{name(ep)}: " + (f"saturates at concurrency {s['concurrency']} ({s['reason'].replace('_', ' ')})" if s else "no saturation in the tested range")
                             + (f"; {ld['info']['aborted_reason']}" if ld["info"].get("aborted_reason") else ""))
        nb.append({"type": "bullets", "items": sat_lines})
        err_rows = []
        for ep, ld in ld_list:
            cls: dict[str, int] = {}
            for s in ld["steps"]:
                for k, v in (s["error_classes"] or {}).items():
                    cls[k] = cls.get(k, 0) + v
            for k, v in sorted(cls.items()):
                err_rows.append([name(ep), k, v])
        if err_rows:
            nb.append(_t(["Endpoint", "Error class", "Count"], err_rows, "Error classes"))
    else:
        nb.append(_p("No succeeded load runs are attached."))
    sections.append({"id": "non_functional", "title": "Non-functional results", "blocks": nb})

    # 7 ---- disagreements
    db_ = []
    if data["disagreements"]:
        hdr = ["#", "Segment", "Distance", "Reference"] + [name(r["endpoint_id"]) for r in ev]
        rows = []
        for i, d in enumerate(data["disagreements"]):
            by = {h["run_id"]: h["text"] for h in d["hypotheses"]}
            rows.append([i + 1, f"{d['segment_id'][:8]} @ {d['start_s']:.1f}s", d["distance"], d["reference"] if d["reference"] is not None else "—"]
                        + [by.get(r["id"], "—") for r in ev])
        db_.append(_t(hdr, rows))
    sections.append({"id": "disagreements", "title": "Disagreement highlights", "blocks": db_ or [_p("Needs at least two evaluated endpoints.")]})

    # 8 ---- decision matrix
    comp_cols = ["Endpoint"] + [f"{COMPONENT_LABEL[k]} ({weights[k]:.0%})" for k in DEFAULT_WEIGHTS] + ["Score"]
    rows = []
    for rk in ranking:
        c = comps[rk["endpoint_id"]]
        rows.append([name(rk["endpoint_id"])] + [c[k] for k in DEFAULT_WEIGHTS] + [rk["score"]])
    db2 = [_t(comp_cols, rows, "Ranked (weights editable in the UI; components are 0–1, higher is better)"),
           {"type": "bullets", "items": [
               "Accuracy = 1 − WER (evaluation runs only).",
               "Code-switch = mean of (1 − WER-AR), (1 − WER-EN), EN-preservation.",
               "Throughput = best error-free RTFx (error rate ≤ 1%) relative to the best endpoint.",
               "Robustness = mean of (1 − hallucination rate), (1 − insertion rate), (1 − failed-segment share).",
               "Missing components are excluded and the remaining weights renormalized."]}]
    sections.append({"id": "decision", "title": "Decision matrix", "blocks": db2})

    # 9 ---- reproduce
    lines = []
    for rd in data["runs"]:
        ref = f"--gold {rd['reference_id']}" if rd["reference_type"] == "gold_set" else f"--ref-run {rd['reference_id']}"
        lines.append(f"mizan run eval --endpoint {rd['endpoint_id']} {ref} --normalizer {rd['normalizer'].get('kind')} --metrics {rd['options'].get('metric_preset', 'arabic_codeswitch_default')}"
                     f" --concurrency {rd['options'].get('concurrency')}")
    for ld in data["loads"]:
        lines.append(f"mizan run load --endpoint {ld['model_endpoint_id']} --scenario {ld['scenario_id']} --dataset {ld['dataset_id']}")
    rep_blocks = [{"type": "code", "text": "\n".join(lines) or "# nothing to reproduce"}]
    rep_blocks.append(_p("Each run's fully materialized configuration (adapter version, model, normalizer rules, metric versions) is available at "
                         "GET /api/v1/runs/{id}/resolved-config; identical configs yield identical metrics."))
    sections.append({"id": "reproduce", "title": "Reproduce", "blocks": rep_blocks})

    # 10 --- appendix
    ab = []
    for rd in ev:
        rows = [[x["stratum_key"], x["stratum_value"], x["metric_id"], x["value"], x["n"]] for x in sorted(
            rd["rows"], key=lambda x: (x["stratum_key"] != "overall", x["stratum_key"], x["stratum_value"], x["metric_id"]))]
        ab.append(_t(["Stratum", "Value", "Metric", "Value", "n"], rows, f"{name(rd['endpoint_id'])} — all strata"))
    ab.append(_p("Raw tables are available as CSV via GET /api/v1/reports/{id}/export?format=csv."))
    sections.append({"id": "appendix", "title": "Appendix", "blocks": ab})

    header = []
    if gold:
        header.append(f"**Gold set:** {gold['name']} v{gold['version']} ({gold['minutes']:.1f} min, {gold['segment_count']} segments)")
    return {"title": data["campaign"]["name"], "question": data["campaign"]["question"], "header": header,
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(), "sections": sections,
            "decision": {"weights": weights, "components": comps, "ranking": ranking, "facts": facts,
                         "endpoint_names": {ep: e["name"] for ep, e in eps.items()}},
            "summary_text": summary_text, "summary_edited": bool(options.get("summary_text"))}


def apply_weights(doc: dict, weights: dict) -> dict:
    """Re-rank from stored components (no DB): replace the decision table, ranking and (if unedited) the summary."""
    w = normalize_weights(weights)
    dec = doc["decision"]
    ranking = rank(dec["components"], w)
    names = dec["endpoint_names"]
    sec = next(s for s in doc["sections"] if s["id"] == "decision")
    cols = ["Endpoint"] + [f"{COMPONENT_LABEL[k]} ({w[k]:.0%})" for k in DEFAULT_WEIGHTS] + ["Score"]
    rows = [[names[r["endpoint_id"]]] + [dec["components"][r["endpoint_id"]][k] for k in DEFAULT_WEIGHTS] + [r["score"]] for r in ranking]
    sec["blocks"][0] = _t(cols, rows, sec["blocks"][0].get("title"))
    dec.update(weights=w, ranking=ranking)
    if not doc.get("summary_edited"):
        doc["summary_text"] = draft_summary(dec["facts"], ranking)
        next(s for s in doc["sections"] if s["id"] == "summary")["blocks"][0] = _p(doc["summary_text"])
    return doc


def to_model(rep: m.Report) -> ReportModel:
    d = rep.sections
    return ReportModel(title=d["title"], question=d.get("question", ""), generated_at=rep.generated_at,
                       sections=d["sections"], meta={"header": d.get("header", []), "report_id": rep.id, "version": rep.version})


async def build_and_store(c, report_id: str) -> None:
    async with c.session() as db:
        rep = await db.get(m.Report, report_id)
        cp = await db.get(m.Campaign, rep.campaign_id)
        try:
            data = await collect(db, cp, rep.options)
            doc = build_report(data, rep.options)
            rep.sections, rep.status, rep.generated_at, rep.error = doc, "ready", utcnow(), None
        except Exception as e:
            rep.status, rep.error = "failed", f"{type(e).__name__}: {e}"
            await db.commit()
            raise
        await db.commit()
