from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.db import utcnow
from mizan.core.errors import Conflict, Invalid
from mizan.core.jobs import enqueue
from mizan.core.services.common import Actor, audit, must_get
from mizan.plugins import registry as plugins
from mizan.workers.load import bucket_range, saturation_point

PRESET = {"name": "ramp-1-64", "concurrency_steps": [1, 2, 4, 8, 16, 32, 48, 64], "step_duration_s": 120,
          "length_buckets": {"5-15s": 0.6, "15-30s": 0.4}, "warmup_s": 30, "timeout_s": 120}


def scenario_view(s: m.LoadScenario) -> dict:
    return {"id": s.id, "name": s.name, "concurrency_steps": s.concurrency_steps, "step_duration_s": s.step_duration_s,
            "length_buckets": s.length_buckets, "warmup_s": s.warmup_s, "timeout_s": s.timeout_s}


async def create_scenario(db: AsyncSession, actor: Actor, req: dict) -> m.LoadScenario:
    steps = req.get("concurrency_steps") or []
    if not steps or any(int(x) < 1 for x in steps):
        raise Invalid("concurrency_steps must be a non-empty list of positive integers")
    buckets = req.get("length_buckets") or {"5-15s": 1.0}
    try:
        for k in buckets:
            bucket_range(k)
    except ValueError as e:
        raise Invalid(f"bad length bucket label: {e}") from e
    if not buckets or sum(buckets.values()) <= 0:
        raise Invalid("length_buckets weights must sum to > 0")
    s = m.LoadScenario(name=req["name"], concurrency_steps=[int(x) for x in steps],
                       step_duration_s=float(req.get("step_duration_s", 120)), length_buckets=buckets,
                       warmup_s=float(req.get("warmup_s", 30)), timeout_s=float(req.get("timeout_s", 120)))
    db.add(s)
    await db.flush()
    audit(db, actor, "loadscenario.create", "load_scenario", s.id, {"name": s.name})
    return s


async def seed_presets(db: AsyncSession) -> None:
    if (await db.execute(select(m.LoadScenario.id).where(m.LoadScenario.name == PRESET["name"]))).first() is None:
        db.add(m.LoadScenario(**PRESET))


async def create_load_run(c, db: AsyncSession, actor: Actor, req: dict) -> m.LoadRun:
    ep = await must_get(db, m.ModelEndpoint, req["model_endpoint_id"], "ModelEndpoint")
    sc = await must_get(db, m.LoadScenario, req["scenario_id"], "LoadScenario")
    ds = await must_get(db, m.Dataset, req["dataset_id"], "Dataset")
    tele = req.get("telemetry_source")
    if tele and not plugins.telemetry_sources.has(tele):
        raise Invalid(f"unknown telemetry source '{tele}'", available=plugins.telemetry_sources.keys())
    n = (await db.execute(select(func.count()).select_from(m.Segment).join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
                          .where(m.AudioFile.dataset_id == ds.id))).scalar()
    if not n:
        raise Invalid("dataset has no segments; upload and segment audio first")
    lr = m.LoadRun(project_id=ds.project_id, model_endpoint_id=ep.id, scenario_id=sc.id, dataset_id=ds.id,
                   telemetry_source=tele, created_by=actor.id)
    db.add(lr)
    await db.flush()
    await enqueue(c, db, "load_run", {"load_run_id": lr.id}, actor.id)
    audit(db, actor, "loadrun.create", "load_run", lr.id, {"endpoint": ep.id, "scenario": sc.id})
    return lr


async def control(c, db: AsyncSession, actor: Actor, lid: str, action: str) -> m.LoadRun:
    lr = await must_get(db, m.LoadRun, lid, "LoadRun")
    allowed = {"cancel": ("pending", "running", "paused"), "pause": ("running",), "resume": ("paused",)}
    if action not in allowed or lr.status not in allowed[action]:
        raise Conflict(f"cannot {action} a {lr.status} load run")
    lr.status = {"cancel": "cancelled", "pause": "paused", "resume": "running"}[action]
    if action == "cancel":
        lr.finished_at = utcnow()
    audit(db, actor, f"loadrun.{action}", "load_run", lid)
    await db.commit()
    await c.hub.publish(f"load-run:{lid}", {"type": "status", "status": lr.status})
    return lr


def summary_view(s: m.LoadSummary) -> dict:
    return {"step": s.step, "concurrency": s.concurrency, "p50": s.p50, "p95": s.p95, "p99": s.p99, "rtfx": s.rtfx,
            "req_per_s": s.req_per_s, "error_rate": s.error_rate, "timeouts": s.timeouts, "requests": s.requests,
            "error_classes": s.error_classes, "gpu": s.gpu, "t_start": s.t_start, "t_end": s.t_end}


async def detail(db: AsyncSession, lid: str) -> dict:
    lr = await must_get(db, m.LoadRun, lid, "LoadRun")
    sums = (await db.execute(select(m.LoadSummary).where(m.LoadSummary.load_run_id == lid).order_by(m.LoadSummary.step))).scalars().all()
    n = (await db.execute(select(func.count()).select_from(m.LoadSample).where(m.LoadSample.load_run_id == lid))).scalar()
    bad = (await db.execute(select(func.count()).select_from(m.LoadSample).where(m.LoadSample.load_run_id == lid, m.LoadSample.ok.is_(False)))).scalar()
    sat = lr.info.get("saturation")
    if sat is None and sums:
        sat = saturation_point([summary_view(s) for s in sums])
    return {"id": lr.id, "project_id": lr.project_id, "model_endpoint_id": lr.model_endpoint_id, "scenario_id": lr.scenario_id,
            "dataset_id": lr.dataset_id, "telemetry_source": lr.telemetry_source, "status": lr.status, "info": lr.info,
            "started_at": lr.started_at, "finished_at": lr.finished_at, "created_at": lr.created_at,
            "steps": [summary_view(s) for s in sums], "saturation": sat, "live": {"requests": n, "errors": bad}}


async def samples(db: AsyncSession, lid: str, step: int | None, limit: int, offset: int) -> dict:
    q = select(m.LoadSample).where(m.LoadSample.load_run_id == lid).order_by(m.LoadSample.id)
    if step is not None:
        q = q.where(m.LoadSample.step == step)
    rows = (await db.execute(q.offset(offset).limit(limit + 1))).scalars().all()
    return {"items": [{"ts": r.ts, "step": r.step, "segment_id": r.segment_id, "latency_ms": r.latency_ms, "ok": r.ok,
                       "error_class": r.error_class, "audio_s": r.audio_s} for r in rows[:limit]],
            "next_cursor": str(offset + limit) if len(rows) > limit else None}


async def telemetry(db: AsyncSession, lid: str) -> dict:
    rows = (await db.execute(select(m.TelemetrySample).where(m.TelemetrySample.load_run_id == lid).order_by(m.TelemetrySample.ts))).scalars().all()
    sums = (await db.execute(select(m.LoadSummary).where(m.LoadSummary.load_run_id == lid).order_by(m.LoadSummary.step))).scalars().all()
    return {"samples": [{"ts": r.ts, "device": r.device, "util_pct": r.util_pct, "mem_used_mb": r.mem_used_mb, "temp_c": r.temp_c,
                         "power_w": r.power_w} for r in rows],
            "steps": [{"step": s.step, "concurrency": s.concurrency, "t_start": s.t_start, "t_end": s.t_end} for s in sums]}
