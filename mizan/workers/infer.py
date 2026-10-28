"""Run orchestrator: one idempotent unit of work per (run, segment); scoring inline; crash/cancel/pause safe."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import delete, select, update

from mizan.core import models as m
from mizan.core.db import utcnow
from mizan.core.jobs import handler
from mizan.core.services.segaudio import SegmentAudioReader
from mizan.core.services import registry as regsvc
from mizan.plugins import registry as plugins
from mizan.plugins.base import AdapterError, AudioInput
from mizan.plugins.metrics.core import _pct
from mizan.plugins.textutil import infer_spans, lang_mix
from mizan.workers.control import StatusControl
from mizan.workers.score import aggregate_run, load_strategies, score_segment

log = logging.getLogger("mizan.infer")
MAX_ATTEMPTS = 3
FAIL_FAST_AFTER = 20  # consecutive failures with zero successes => endpoint is down
FLUSH_EVERY_S = 1.0


@dataclass
class SegInfo:
    seg: m.Segment
    ref_text: str | None = None
    ref_spans: list | None = None
    flags: list = field(default_factory=list)
    strata: dict = field(default_factory=dict)


def RunControl(c, run_id: str) -> StatusControl:
    return StatusControl(c, m.Run, run_id)


class Buffer:
    """Batches hypothesis/score writes; replaces rows for (run, segment) so retries and resumes are idempotent."""

    def __init__(self, c, run_id: str):
        self.c, self.run_id = c, run_id
        self.hyps: list[m.Hypothesis] = []
        self.scores: list[m.SegmentScore] = []
        self.lock = asyncio.Lock()

    async def add(self, h: m.Hypothesis, scores: list[m.SegmentScore]) -> None:
        async with self.lock:
            self.hyps.append(h)
            self.scores.extend(scores)

    async def flush(self) -> None:
        async with self.lock:
            hyps, scores = self.hyps, self.scores
            self.hyps, self.scores = [], []
        if not hyps:
            return
        sids = [h.segment_id for h in hyps]
        async with self.c.session() as db:
            for i in range(0, len(sids), 400):
                chunk = sids[i:i + 400]
                await db.execute(delete(m.SegmentScore).where(m.SegmentScore.run_id == self.run_id, m.SegmentScore.segment_id.in_(chunk)))
                await db.execute(delete(m.Hypothesis).where(m.Hypothesis.run_id == self.run_id, m.Hypothesis.segment_id.in_(chunk)))
            db.add_all(hyps)
            db.add_all(scores)
            await db.commit()


# ------------------------------------------------------------ segment selection
def _match_filter(strata: dict, flags: list, f: dict) -> bool:
    for k, want in (f or {}).items():
        if k == "exclude_flags":
            if set(want) & set(flags):
                return False
        elif k == "only_flags":
            if not set(want) & set(flags):
                return False
        else:
            wants = want if isinstance(want, list) else [want]
            if str(strata.get(k)) not in {str(w) for w in wants}:
                return False
    return True


async def select_segments(db, run: m.Run) -> list[SegInfo]:
    flt = run.options.get("segment_filter") or {}
    infos: list[SegInfo] = []
    if run.reference_type == "gold_set":
        q = (select(m.GoldItem, m.Segment).join(m.Segment, m.Segment.id == m.GoldItem.segment_id)
             .where(m.GoldItem.gold_set_id == run.reference_id).order_by(m.Segment.audio_file_id, m.Segment.idx))
        for it, seg in (await db.execute(q)).all():
            if _match_filter(it.strata, it.flags, flt):
                infos.append(SegInfo(seg, it.text, it.spans, list(it.flags), dict(it.strata)))
    elif run.reference_type == "run":
        q = (select(m.Hypothesis, m.Segment).join(m.Segment, m.Segment.id == m.Hypothesis.segment_id)
             .where(m.Hypothesis.run_id == run.reference_id, m.Hypothesis.error.is_(None)).order_by(m.Segment.audio_file_id, m.Segment.idx))
        for h, seg in (await db.execute(q)).all():
            if _match_filter(seg.strata, seg.flags, flt):
                infos.append(SegInfo(seg, h.text, h.spans, list(seg.flags), dict(seg.strata)))
    else:
        q = (select(m.Segment).join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
             .where(m.AudioFile.dataset_id == run.dataset_id, m.AudioFile.status == "ready")
             .order_by(m.Segment.audio_file_id, m.Segment.idx))
        for seg in (await db.execute(q)).scalars():
            if _match_filter(seg.strata, seg.flags, flt):
                infos.append(SegInfo(seg, None, None, list(seg.flags), dict(seg.strata)))
        if run.type == "bootstrap":
            # never touch segments with human work; skip other runs' machine drafts unless overwrite_drafts;
            # segments this run already drafted stay in scope so a resumed run keeps a stable total.
            A = m.Annotation
            human = set((await db.execute(select(A.segment_id).where(
                A.track == "primary", (A.author_id.is_not(None)) | (A.status != "draft")
                | (~A.source.like("machine:%") & (A.text != ""))))).scalars())
            machine = set((await db.execute(select(A.segment_id).where(
                A.track == "primary", A.source.like("machine:%"), A.text != ""))).scalars())
            mine = set((await db.execute(select(m.Hypothesis.segment_id).where(m.Hypothesis.run_id == run.id))).scalars())
            keep_machine = bool(run.options.get("overwrite_drafts"))
            infos = [i for i in infos if i.seg.id not in human and (keep_machine or i.seg.id in mine or i.seg.id not in machine)]
    if run.options.get("limit"):
        infos = infos[: int(run.options["limit"])]
    return infos


# ------------------------------------------------------------------ the handler
@handler("run")
async def execute_run(c, job, ctx) -> dict:
    run_id = job.payload["run_id"]
    async with c.session() as db:
        run = await db.get(m.Run, run_id)
        if run is None or run.status in ("cancelled", "succeeded"):
            return {"_cancelled": run is not None and run.status == "cancelled"}
        ep = await db.get(m.ModelEndpoint, run.model_endpoint_id)
        adapter = await regsvc.adapter_for(db, ep)
        infos = await select_segments(db, run)
        done_ok = set((await db.execute(select(m.Hypothesis.segment_id).where(
            m.Hypothesis.run_id == run_id, m.Hypothesis.error.is_(None)))).scalars())
        run.status, run.started_at, run.error = "running", run.started_at or utcnow(), None
        run.progress_total = len(infos)
        await db.commit()
        await db.refresh(run)
        db.expunge(run)
    scored = run.type in ("evaluation", "agreement")
    norm, metrics = load_strategies(run.normalizer_cfg, run.metric_set) if scored else (None, {})
    opts = run.options
    ctl, buf = RunControl(c, run_id), Buffer(c, run_id)
    reader = SegmentAudioReader(c)
    todo = [i for i in infos if i.seg.id not in done_ok]
    async with c.session() as db:
        await reader.prepare(db, [i.seg for i in todo])
    total, state = len(infos), {"done": len(infos) - len(todo), "ok": 0, "fail": 0, "consec_fail": 0, "audio_s": 0.0,
                                 "lat": deque(maxlen=200), "last": None}
    t_start = time.perf_counter()
    q: asyncio.Queue[SegInfo | None] = asyncio.Queue()
    for i in todo:
        q.put_nowait(i)
    fatal: list[str] = []

    async with c.session() as db:  # preflight: warning only (some servers have no /v1/models)
        try:
            health = await adapter.health()
        except Exception as e:
            health = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
        r = await db.get(m.Run, run_id)
        r.resolved_config = {**r.resolved_config, "preflight": {**health, "at": utcnow().isoformat()}}
        await db.commit()

    async def worker() -> None:
        while not fatal:
            try:
                info = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not await ctl.checkpoint():
                return
            seg = info.seg
            try:
                wav = await reader.wav(seg)
            except Exception as e:
                await _record(info, None, f"audio:{type(e).__name__}: {e}", 0)
                continue
            audio = AudioInput(wav, seg.duration_s, seg.id, opts.get("language_hint"))
            err: str | None = None
            res = None
            attempt = 0
            while attempt < MAX_ATTEMPTS:
                attempt += 1
                try:
                    res = await adapter.transcribe(audio, {"timeout_s": opts.get("timeout_s", 120), **opts})
                    err = None
                    break
                except AdapterError as e:
                    err = e.error_class
                    if not e.retryable or attempt >= MAX_ATTEMPTS:
                        break
                    await asyncio.sleep(min(8, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random() / 2))
                except Exception as e:  # adapter bug: treat as non-retryable, keep the run alive
                    err = f"{type(e).__name__}"
                    log.exception("adapter crashed on segment %s", seg.id)
                    break
            await _record(info, res, err, attempt)

    async def _record(info: SegInfo, res, err: str | None, attempt: int) -> None:
        seg = info.seg
        if res is not None:
            hyp_spans = res.spans or infer_spans(res.text)
            h = m.Hypothesis(run_id=run_id, segment_id=seg.id, text=res.text, spans=hyp_spans, latency_ms=res.latency_ms,
                             raw=res.raw if isinstance(res.raw, dict) else {"value": res.raw}, error=None, attempt=attempt)
            scores = []
            if scored:
                ctx_ = {"latency_ms": res.latency_ms, "audio_s": seg.duration_s}
                for mid, r_ in score_segment(norm, metrics, info.ref_text, info.ref_spans, res.text, hyp_spans, info.flags, ctx_).items():
                    scores.append(m.SegmentScore(run_id=run_id, segment_id=seg.id, metric_id=mid, value=r_.value,
                                                 details=r_.details, applicable=r_.applicable))
            await buf.add(h, scores)
            state["ok"] += 1
            state["consec_fail"] = 0
            state["audio_s"] += seg.duration_s
            state["lat"].append(res.latency_ms)
            if run.type == "bootstrap":
                await _write_draft(c, run, seg, res.text)
        else:
            await buf.add(m.Hypothesis(run_id=run_id, segment_id=seg.id, text="", error=err or "unknown", attempt=attempt, raw={}), [])
            state["fail"] += 1
            state["consec_fail"] += 1
            if state["consec_fail"] >= FAIL_FAST_AFTER and state["ok"] == 0:
                fatal.append(f"endpoint unavailable: {state['consec_fail']} consecutive failures (last: {err})")
        state["done"] += 1
        state["last"] = seg.id

    async def ticker() -> None:
        while True:
            await asyncio.sleep(FLUSH_EVERY_S)
            await buf.flush()
            elapsed = time.perf_counter() - t_start
            n_new = state["ok"] + state["fail"]
            eta = (len(todo) - n_new) / (n_new / elapsed) if n_new and elapsed else None
            lat = sorted(state["lat"])
            async with c.session() as db:
                await db.execute(update(m.Run).where(m.Run.id == run_id).values(progress_done=state["done"]))
                await db.commit()
            await c.hub.publish(f"run:{run_id}", {"type": "progress", "done": state["done"], "total": total, "eta_s": eta,
                                                  "last_segment_id": state["last"], "failed": state["fail"],
                                                  "p95_ms": _pct(lat, 95) if lat else None})
            await ctx.progress(state["done"], total)

    tick = asyncio.create_task(ticker())
    status, error = "succeeded", None
    try:
        n_workers = max(1, min(int(opts.get("concurrency") or 8), max(1, len(todo))))
        await asyncio.gather(*(worker() for _ in range(n_workers)))
    except asyncio.CancelledError:
        tick.cancel()
        await buf.flush()
        ctl.close()
        await adapter.aclose()
        raise  # process shutdown: job is requeued, run resumes from the stored hypotheses
    except Exception as e:
        status, error = "failed", f"{type(e).__name__}: {e}"
        log.exception("run %s crashed", run_id)
    finally:
        tick.cancel()
    await buf.flush()
    wall = time.perf_counter() - t_start
    if fatal:
        status, error = "failed", fatal[0]
    elif status == "succeeded" and todo and state["ok"] == 0 and state["fail"] > 0 and ctl.state != "cancelled":
        status, error = "failed", f"all {state['fail']} segments failed (last error recorded on the hypotheses)"
    elif ctl.state == "cancelled":
        status = "cancelled"
    ctl.close()
    await adapter.aclose()

    summary_rows: list[dict] = []
    if status == "succeeded" and scored:
        summary_rows = await _summarize(c, run, infos, metrics)
    async with c.session() as db:
        r = await db.get(m.Run, run_id)
        rt = dict(r.resolved_config.get("runtime", {}))
        rt["infer_wall_s"] = rt.get("infer_wall_s", 0.0) + wall
        r.resolved_config = {**r.resolved_config, "runtime": rt}
        if r.status != "cancelled":
            r.status = status
        r.error, r.finished_at = error, utcnow()
        r.progress_done = state["done"]
        if status in ("succeeded",) or summary_rows:
            ok_audio = (await db.execute(select(m.Segment.start_s, m.Segment.end_s).join(m.Hypothesis, m.Hypothesis.segment_id == m.Segment.id)
                                         .where(m.Hypothesis.run_id == run_id, m.Hypothesis.error.is_(None)))).all()
            audio_s = sum(e - s for s, e in ok_audio)
            await db.execute(delete(m.RunSummary).where(m.RunSummary.run_id == run_id, m.RunSummary.metric_id.in_(
                ("rtfx_observed", "audio_s", "segments_ok", "segments_failed", "elapsed_s"))))
            nfail = len(infos) - len(ok_audio)
            extra = {"rtfx_observed": audio_s / rt["infer_wall_s"] if rt["infer_wall_s"] else 0.0, "audio_s": audio_s,
                     "segments_ok": float(len(ok_audio)), "segments_failed": float(nfail), "elapsed_s": rt["infer_wall_s"]}
            for k, v in extra.items():
                db.add(m.RunSummary(run_id=run_id, stratum_key="overall", stratum_value="all", metric_id=k, value=v, n=len(ok_audio)))
        await db.commit()
    await c.hub.publish(f"run:{run_id}", {"type": "finished", "status": status, "error": error})
    return {"_cancelled": True} if status == "cancelled" else ({"run_status": status, "error": error} if error else {"run_status": status})


async def _summarize(c, run: m.Run, infos: list[SegInfo], metrics: dict) -> list[dict]:
    """Corpus-level aggregation over stored segment scores (re-readable after resume)."""
    from mizan.plugins.base import MetricResult

    strata = {i.seg.id: i.strata for i in infos}
    async with c.session() as db:
        rows = (await db.execute(select(m.SegmentScore).where(m.SegmentScore.run_id == run.id))).scalars().all()
        by_seg: dict[str, dict] = {}
        for s in rows:
            by_seg.setdefault(s.segment_id, {})[s.metric_id] = MetricResult(s.value, s.details, s.applicable)
        data = [{"segment_id": sid, "strata": strata.get(sid, {}), "scores": sc} for sid, sc in by_seg.items()]
        summ = aggregate_run(metrics, data)
        await db.execute(delete(m.RunSummary).where(m.RunSummary.run_id == run.id))
        db.add_all(m.RunSummary(run_id=run.id, **r) for r in summ)
        await db.commit()
    return summ


async def _write_draft(c, run: m.Run, seg: m.Segment, text: str) -> None:
    """Bootstrap: machine draft becomes version 1 (or fills an untouched blank placeholder)."""
    from mizan.plugins.textutil import nfc

    text = nfc(text)
    async with c.session() as db:
        cur = (await db.execute(select(m.Annotation).where(m.Annotation.segment_id == seg.id, m.Annotation.track == "primary")
                                .order_by(m.Annotation.version.desc()).limit(1))).scalar_one_or_none()
        source = f"machine:{run.model_endpoint_id}"
        spans = infer_spans(text)
        if cur is None:
            db.add(m.Annotation(segment_id=seg.id, version=1, text=text, spans=spans, status="draft", source=source))
        elif cur.status == "draft" and cur.author_id is None and (not cur.text or cur.source.startswith("machine:")):
            cur.text, cur.spans, cur.source = text, spans, source
        else:
            return
        if text.strip() == "":
            pass
        await db.commit()
