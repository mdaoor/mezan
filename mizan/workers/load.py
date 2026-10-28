"""Load generator + telemetry collector (spec 8.4)."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from mizan.core import models as m
from mizan.core.db import utcnow
from mizan.core.jobs import handler
from mizan.core.services import registry as regsvc
from mizan.core.services.segaudio import SegmentAudioReader
from mizan.plugins import registry as plugins
from mizan.plugins.base import AdapterError, AudioInput
from mizan.plugins.metrics.core import _pct
from mizan.plugins.textutil import duration_bucket
from mizan.workers.control import StatusControl

log = logging.getLogger("mizan.load")
ABORT_ERROR_RATE = 0.20
ABORT_MIN_REQUESTS = 10
PRELOAD_PER_BUCKET = 48


def bucket_range(label: str) -> tuple[float, float]:
    label = label.strip().rstrip("s")
    if label.startswith("<"):
        return 0.0, float(label[1:])
    if label.startswith(">"):
        return float(label[1:]), float("inf")
    lo, _, hi = label.partition("-")
    return float(lo), float(hi)


def saturation_point(summaries: list[dict]) -> dict | None:
    """First step where p95 > 2x the previous step OR error rate > 1 %."""
    prev = None
    for s in summaries:
        if s["error_rate"] > 0.01:
            return {"step": s["step"], "concurrency": s["concurrency"], "reason": "error_rate"}
        if prev is not None and prev["p95"] > 0 and s["p95"] > 2 * prev["p95"]:
            return {"step": s["step"], "concurrency": s["concurrency"], "reason": "p95_growth"}
        prev = s
    return None


def summarize_step(step: int, conc: int, samples: list[dict], duration_s: float) -> dict:
    ok = sorted(x["latency_ms"] for x in samples if x["ok"])
    n = len(samples)
    errs: dict[str, int] = {}
    for x in samples:
        if not x["ok"]:
            errs[x["error_class"] or "unknown"] = errs.get(x["error_class"] or "unknown", 0) + 1
    return {"step": step, "concurrency": conc, "p50": _pct(ok, 50), "p95": _pct(ok, 95), "p99": _pct(ok, 99),
            "rtfx": sum(x["audio_s"] for x in samples if x["ok"]) / duration_s if duration_s else 0.0,
            "req_per_s": n / duration_s if duration_s else 0.0, "error_rate": (n - len(ok)) / n if n else 0.0,
            "timeouts": errs.get("timeout", 0), "requests": n, "error_classes": errs}


def gpu_aggregate(tele: list[m.TelemetrySample], t0: datetime, t1: datetime) -> dict:
    per: dict[str, dict] = {}
    for t in tele:
        if not (t0 <= t.ts <= t1):
            continue
        d = per.setdefault(t.device, {"util": [], "mem": [], "temp": [], "power": []})
        for k, v in (("util", t.util_pct), ("mem", t.mem_used_mb), ("temp", t.temp_c), ("power", t.power_w)):
            if v is not None:
                d[k].append(v)
    return {dev: {"util_avg": sum(d["util"]) / len(d["util"]) if d["util"] else None, "util_max": max(d["util"], default=None),
                  "mem_used_mb_max": max(d["mem"], default=None), "temp_c_max": max(d["temp"], default=None),
                  "power_w_max": max(d["power"], default=None)} for dev, d in per.items()}


@handler("load_run")
async def execute_load(c, job, ctx) -> dict:
    lid = job.payload["load_run_id"]
    async with c.session() as db:
        lr = await db.get(m.LoadRun, lid)
        if lr is None or lr.status in ("cancelled", "succeeded"):
            return {"_cancelled": lr is not None and lr.status == "cancelled"}
        sc = await db.get(m.LoadScenario, lr.scenario_id)
        ep = await db.get(m.ModelEndpoint, lr.model_endpoint_id)
        adapter = await regsvc.adapter_for(db, ep)
        segs = (await db.execute(select(m.Segment).join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
                                 .where(m.AudioFile.dataset_id == lr.dataset_id, m.AudioFile.status == "ready")
                                 .order_by(m.Segment.id))).scalars().all()
        if not segs:
            raise RuntimeError("dataset has no segments to sample for load generation")
        await db.execute(delete(m.LoadSample).where(m.LoadSample.load_run_id == lid))
        await db.execute(delete(m.LoadSummary).where(m.LoadSummary.load_run_id == lid))
        await db.execute(delete(m.TelemetrySample).where(m.TelemetrySample.load_run_id == lid))
        lr.status, lr.started_at = "running", utcnow()
        await db.commit()
        steps = list(sc.concurrency_steps)
        cap = ep.config.get("max_concurrency")
        steps = [min(int(s), int(cap)) if cap else int(s) for s in steps]
        tele_kind = lr.telemetry_source or "none"
        info = {"scenario": {"name": sc.name, "concurrency_steps": sc.concurrency_steps, "effective_steps": steps,
                             "step_duration_s": sc.step_duration_s, "length_buckets": sc.length_buckets,
                             "warmup_s": sc.warmup_s, "timeout_s": sc.timeout_s}, "telemetry_source": tele_kind}

    # ---- sample pool by length bucket, audio preloaded (the generator must not be disk-bound)
    reader = SegmentAudioReader(c)
    buckets = {label: bucket_range(label) for label in sc.length_buckets}
    pool: dict[str, list[tuple[bytes, float, str]]] = {}
    rng = random.Random(job.id)
    for label, (lo, hi) in buckets.items():
        cand = [s for s in segs if lo <= s.duration_s < hi] or sorted(segs, key=lambda s: abs(s.duration_s - (lo + min(hi, lo + 20)) / 2))[:PRELOAD_PER_BUCKET]
        rng.shuffle(cand)
        cand = cand[:PRELOAD_PER_BUCKET]
        pool[label] = cand
    async with c.session() as db:
        await reader.prepare(db, [s for v in pool.values() for s in v])
    loaded: dict[str, list[tuple[bytes, float, str]]] = {
        label: [(await reader.wav(s), s.duration_s, s.id) for s in v] for label, v in pool.items()}
    labels = list(sc.length_buckets)
    weights = [sc.length_buckets[k] for k in labels]

    def pick() -> tuple[bytes, float, str]:
        return rng.choice(loaded[rng.choices(labels, weights)[0]])

    ctl = StatusControl(c, m.LoadRun, lid)
    samples_buf: list[m.LoadSample] = []
    tele_buf: list[m.TelemetrySample] = []
    tele_all: list[m.TelemetrySample] = []
    stop_tele = asyncio.Event()
    source = plugins.telemetry_sources.get(tele_kind)(ep.config.get("telemetry", {})) if plugins.telemetry_sources.has(tele_kind) else None

    async def tele_loop() -> None:
        while not stop_tele.is_set():
            try:
                for t in (await source.sample()) if source else []:
                    row = m.TelemetrySample(load_run_id=lid, ts=t.ts, device=t.device, util_pct=t.util_pct,
                                            mem_used_mb=t.mem_used_mb, temp_c=t.temp_c, power_w=t.power_w)
                    tele_buf.append(row)
                    tele_all.append(row)
            except Exception as e:
                log.debug("telemetry error: %s", e)
            try:
                await asyncio.wait_for(stop_tele.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    async def flush() -> None:
        s, t = samples_buf[:], tele_buf[:]
        del samples_buf[: len(s)], tele_buf[: len(t)]
        if s or t:
            async with c.session() as db:
                db.add_all(s + t)
                await db.commit()

    async def flush_loop() -> None:
        while True:
            await asyncio.sleep(1.0)
            await flush()

    async def one_request(step: int, record: bool, aborted: asyncio.Event, counters: dict, step_samples: list) -> None:
        wav, dur, sid = pick()
        t0 = time.perf_counter()
        ok, err = True, None
        try:
            await asyncio.wait_for(adapter.transcribe(AudioInput(wav, dur, sid), {"timeout_s": sc.timeout_s}), sc.timeout_s)
        except asyncio.TimeoutError:
            ok, err = False, "timeout"
        except AdapterError as e:
            ok, err = False, e.error_class
        except Exception as e:
            ok, err = False, type(e).__name__
        lat = (time.perf_counter() - t0) * 1000
        if not record:
            return
        rec = {"ok": ok, "latency_ms": lat, "error_class": err, "audio_s": dur}
        step_samples.append(rec)
        samples_buf.append(m.LoadSample(load_run_id=lid, ts=datetime.now(timezone.utc), step=step, segment_id=sid,
                                        latency_ms=lat, ok=ok, error_class=err, audio_s=dur))
        counters["n"] += 1
        counters["err"] += 0 if ok else 1
        if counters["n"] >= ABORT_MIN_REQUESTS and counters["err"] / counters["n"] > ABORT_ERROR_RATE:
            aborted.set()

    async def drive(concurrency: int, seconds: float, step: int, record: bool, aborted: asyncio.Event, counters: dict,
                    step_samples: list) -> None:
        deadline = time.perf_counter() + seconds

        async def loop() -> None:
            while time.perf_counter() < deadline and not aborted.is_set() and ctl.state != "cancelled":
                await ctl.checkpoint()
                await one_request(step, record, aborted, counters, step_samples)

        await asyncio.gather(*(loop() for _ in range(concurrency)))

    tele_task, flush_task = asyncio.create_task(tele_loop()), asyncio.create_task(flush_loop())
    status, error, summaries = "succeeded", None, []
    try:
        if sc.warmup_s and steps:
            await drive(steps[0], sc.warmup_s, -1, False, asyncio.Event(), {"n": 0, "err": 0}, [])
        for i, conc in enumerate(steps):
            if ctl.cancelled:
                break
            aborted, counters, step_samples = asyncio.Event(), {"n": 0, "err": 0}, []
            t0 = datetime.now(timezone.utc)
            tt = time.perf_counter()
            await drive(conc, sc.step_duration_s, i, True, aborted, counters, step_samples)
            elapsed = time.perf_counter() - tt
            t1 = datetime.now(timezone.utc)
            s = summarize_step(i, conc, step_samples, elapsed)
            s["gpu"], s["t_start"], s["t_end"] = gpu_aggregate(tele_all, t0 - timedelta(seconds=0.5), t1), t0, t1
            summaries.append(s)
            async with c.session() as db:
                db.add(m.LoadSummary(load_run_id=lid, **s))
                await db.commit()
            await c.hub.publish(f"load-run:{lid}", {"type": "step", "step": i, "concurrency": conc, "p50": s["p50"], "p95": s["p95"],
                                                    "p99": s["p99"], "rps": s["req_per_s"], "errors": s["error_rate"],
                                                    "rtfx": s["rtfx"], "aborted": aborted.is_set()})
            await ctx.progress(i + 1, len(steps), force=True)
            if aborted.is_set():
                info["aborted_reason"] = (f"step {i} (concurrency {conc}) aborted: error rate "
                                          f"{counters['err'] / max(counters['n'], 1):.0%} > {ABORT_ERROR_RATE:.0%}")
                break
    except asyncio.CancelledError:
        raise
    except Exception as e:
        status, error = "failed", f"{type(e).__name__}: {e}"
        log.exception("load run %s crashed", lid)
    finally:
        stop_tele.set()
        flush_task.cancel()
        await asyncio.gather(tele_task, return_exceptions=True)
        await flush()
        ctl.close()
        await adapter.aclose()

    if ctl.state == "cancelled":
        status = "cancelled"
    info["saturation"] = saturation_point([{k: v for k, v in s.items() if k in ("step", "concurrency", "p95", "error_rate")} for s in summaries])
    async with c.session() as db:
        lr = await db.get(m.LoadRun, lid)
        if lr.status != "cancelled":
            lr.status = status
        lr.info, lr.finished_at = {**lr.info, **info, **({"error": error} if error else {})}, utcnow()
        await db.commit()
    await c.hub.publish(f"load-run:{lid}", {"type": "finished", "status": status, "saturation": info["saturation"]})
    return {"_cancelled": True} if status == "cancelled" else {"load_status": status}
