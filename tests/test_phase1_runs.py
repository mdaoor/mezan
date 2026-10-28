import asyncio

import pytest
from sqlalchemy import func, select

import tests.adapters_for_tests as T
from mizan.core import models as m
from mizan.core.jobs import Worker
from tests.conftest import setup_dataset


@pytest.fixture(autouse=True)
def _reset():
    T.CountingAdapter.reset()
    from mizan.workers import infer
    yield


async def make_ep(env, kind="counting", name="EP", **cfg):
    return await env.call("POST", "/model-endpoints", json={"engine_kind": kind, "model_id": cfg.pop("model_id", "m"), "display_name": name,
                                                            "base_url": "x://", "config": cfg})


async def wait_run(env, run_id, timeout=60):
    await env.drain(timeout)
    return await env.call("GET", f"/runs/{run_id}")


async def test_engines_list_new_plugin_without_schema_change(env):
    engines = (await env.call("GET", "/engines"))["items"]
    kinds = {e["kind"]: e for e in engines}
    assert {"vllm_openai", "http_openai_compat", "faster_whisper_http", "fanar_api", "openai_api", "aws_transcribe", "counting"} <= set(kinds)
    assert kinds["counting"]["version"] == "9.9.9" and kinds["counting"]["config_schema"]["required"] == ["model"]
    assert kinds["vllm_openai"]["capabilities"]["concurrency_hint"] == 48 and kinds["fanar_api"]["external"] is True
    assert kinds["vllm_openai"]["external"] is False


async def test_endpoint_validation_masking_and_crud(env):
    await env.call("POST", "/model-endpoints", expect=422, json={"engine_kind": "nope", "model_id": "m", "display_name": "x"})
    await env.call("POST", "/model-endpoints", expect=422, json={"engine_kind": "fanar_api", "model_id": "m", "display_name": "x"})  # api_key required
    await env.call("POST", "/model-endpoints", expect=422, json={"engine_kind": "vllm_openai", "model_id": "m", "display_name": "x",
                                                                 "base_url": "http://a", "config": {"max_concurrency": "many"}})
    ep = await env.call("POST", "/model-endpoints", json={"engine_kind": "fanar_api", "model_id": "Fanar", "display_name": "Fanar",
                                                          "config": {"api_key": "sk-secret"}})
    assert ep["external"] is True and ep["config"]["api_key"] == "***" and "sk-secret" not in str(ep)
    got = await env.call("PATCH", f"/model-endpoints/{ep['id']}", json={"display_name": "Fanar 2", "config": {"api_key": "***", "max_concurrency": 4}})
    assert got["display_name"] == "Fanar 2" and got["config"]["max_concurrency"] == 4
    async with env.c.session() as db:
        stored = await db.get(m.ModelEndpoint, ep["id"])
        assert stored.config["api_key"] == "sk-secret"               # masked value never overwrote the secret
    await env.call("DELETE", f"/model-endpoints/{ep['id']}")
    await env.call("GET", f"/model-endpoints/{ep['id']}", expect=404)


async def test_endpoint_test_endpoint(env):
    ok = await make_ep(env, default_text="مرحبا")
    r = await env.call("POST", f"/model-endpoints/{ok['id']}/test")
    assert r["ok"] and r["smoke"]["text"] == "مرحبا" and r["smoke"]["source"] == "synthetic" and r["smoke"]["latency_ms"] >= 0
    bad = await make_ep(env, name="bad", fail_always=True)
    r = await env.call("POST", f"/model-endpoints/{bad['id']}/test")
    assert not r["ok"] and r["smoke"]["error"] == "http_503"
    assert (await env.call("GET", f"/model-endpoints/{bad['id']}"))["status"] == "error"
    d = await env.call("GET", f"/model-endpoints/{ok['id']}")
    assert d["status"] == "ok" and d["last_health_at"]


async def test_bootstrap_writes_machine_drafts(env):
    s = await setup_dataset(env, n_utts=8)
    ep = await make_ep(env, default_text="مسودة draft")
    r = await env.call("POST", f"/datasets/{s['dataset']['id']}/bootstrap", json={"model_endpoint_id": ep["id"], "options": {"concurrency": 4}})
    run = await wait_run(env, r["run_id"])
    assert run["status"] == "succeeded" and run["type"] == "bootstrap" and run["progress"] == {"done": 8, "total": 8}
    assert not ({"wer", "cer", "mer"} & set(run["summary"]))          # bootstrap runs are never scored
    assert run["summary"]["segments_ok"] == 8 and run["summary"]["segments_failed"] == 0 and run["summary"]["rtfx_observed"] > 0
    segs = (await env.call("GET", f"/datasets/{s['dataset']['id']}/segments"))["items"]
    assert all(x["annotation"]["source"] == f"machine:{ep['id']}" and x["annotation"]["status"] == "draft" and x["annotation"]["text"] == "مسودة draft"
               for x in segs)
    assert T.CountingAdapter.max_inflight <= 4
    # second bootstrap skips segments that already have drafts only if humans touched them; untouched drafts are refreshed
    rc = await env.call("GET", f"/runs/{r['run_id']}/resolved-config")
    assert rc["engine"]["adapter_version"] == "9.9.9" and rc["options"]["concurrency"] == 4 and rc["preflight"]["ok"]


async def test_run_validation(env):
    s = await setup_dataset(env, n_utts=2)
    ep = await make_ep(env)
    await env.call("POST", "/runs", expect=422, json={"type": "evaluation", "model_endpoint_id": ep["id"], "dataset_id": s["dataset"]["id"]})   # needs reference
    await env.call("POST", "/runs", expect=404, json={"type": "evaluation", "model_endpoint_id": ep["id"], "reference": {"type": "run", "id": "x"}})
    await env.call("POST", "/runs", expect=404, json={"type": "evaluation", "model_endpoint_id": ep["id"], "reference": {"type": "gold_set", "id": "x"}})
    await env.call("POST", "/runs", expect=404, json={"type": "bootstrap", "model_endpoint_id": "nope", "dataset_id": s["dataset"]["id"]})
    await env.call("POST", "/runs", expect=422, json={"type": "bootstrap", "model_endpoint_id": ep["id"]})                                      # no dataset


async def test_retry_and_error_recording(env):
    s = await setup_dataset(env, n_utts=4)
    sids = [x["id"] for x in s["segments"]]
    ep = await make_ep(env, fail_first=2, fatal_on=[sids[0]])
    r = await env.call("POST", "/runs", json={"type": "bootstrap", "model_endpoint_id": ep["id"], "dataset_id": s["dataset"]["id"]})
    run = await wait_run(env, r["run_id"])
    assert run["status"] == "succeeded"
    hyps = {h["segment_id"]: h for h in (await env.call("GET", f"/runs/{r['run_id']}/hypotheses"))["items"]}
    assert hyps[sids[0]]["error"] == "http_400" and hyps[sids[0]]["attempt"] == 1          # non-retryable: one attempt
    assert hyps[sids[1]]["error"] is None and hyps[sids[1]]["attempt"] == 3                 # 2 timeouts then success
    assert T.CountingAdapter.calls[sids[0]] == 1 and T.CountingAdapter.calls[sids[1]] == 3
    errs = await env.call("GET", f"/runs/{r['run_id']}/hypotheses?errors_only=true")
    assert len(errs["items"]) == 1


async def test_dead_endpoint_fails_fast(env):
    s = await setup_dataset(env, n_utts=5)
    # ~27 one-second windows so the 20-consecutive-failure guard trips
    await env.call("POST", f"/datasets/{s['dataset']['id']}/segment", json={"segmenter": {"kind": "fixed_window", "config": {"window_s": 1}}})
    await env.drain()
    ep = await make_ep(env, fail_always=True, latency_ms=0)
    r = await env.call("POST", "/runs", json={"type": "bootstrap", "model_endpoint_id": ep["id"], "dataset_id": s["dataset"]["id"], "options": {"concurrency": 8}})
    run = await wait_run(env, r["run_id"], 120)
    assert run["status"] == "failed" and "endpoint unavailable" in run["error"]
    # the same dead endpoint on a tiny run fails with the all-failed message instead
    await env.call("POST", f"/datasets/{s['dataset']['id']}/segment", json={"segmenter": {"kind": "fixed_window", "config": {"window_s": 15}}})
    await env.drain()
    r2 = await env.call("POST", "/runs", json={"type": "bootstrap", "model_endpoint_id": ep["id"], "dataset_id": s["dataset"]["id"]})
    run2 = await wait_run(env, r2["run_id"], 120)
    assert run2["status"] == "failed" and "segments failed" in run2["error"]


async def test_cancel_then_resume_recomputes_only_missing(env):
    s = await setup_dataset(env, n_utts=12)
    await env.call("POST", f"/datasets/{s['dataset']['id']}/segment", json={"segmenter": {"kind": "fixed_window", "config": {"window_s": 2}}})
    await env.drain()
    total = (await env.call("GET", f"/datasets/{s['dataset']['id']}/segments?limit=1000"))["total"]
    ep = await make_ep(env, latency_ms=60)
    T.CountingAdapter.reset()
    r = await env.call("POST", "/runs", json={"type": "bootstrap", "model_endpoint_id": ep["id"], "dataset_id": s["dataset"]["id"], "options": {"concurrency": 2}})
    rid = r["run_id"]
    for _ in range(200):                                                                    # wait until some work is done
        if len(T.CountingAdapter.calls) >= 6:
            break
        await asyncio.sleep(0.05)
    await env.call("POST", f"/runs/{rid}/cancel")
    await env.drain()
    run = await env.call("GET", f"/runs/{rid}")
    assert run["status"] == "cancelled"
    async with env.c.session() as db:
        done = (await db.execute(select(func.count()).select_from(m.Hypothesis).where(m.Hypothesis.run_id == rid))).scalar()
    assert 0 < done < total
    before = dict(T.CountingAdapter.calls)
    await env.call("POST", f"/runs/{rid}/rerun")
    run = await wait_run(env, rid, 120)
    assert run["status"] == "succeeded"
    async with env.c.session() as db:
        n = (await db.execute(select(func.count()).select_from(m.Hypothesis).where(m.Hypothesis.run_id == rid))).scalar()
    assert n == total
    redone = [sid for sid, c in T.CountingAdapter.calls.items() if c > before.get(sid, 0) and sid in before]
    assert len(redone) <= 2                                                                   # only segments that were in flight at cancel


async def test_pause_resume(env):
    s = await setup_dataset(env, n_utts=6)
    await env.call("POST", f"/datasets/{s['dataset']['id']}/segment", json={"segmenter": {"kind": "fixed_window", "config": {"window_s": 2}}})
    await env.drain()
    ep = await make_ep(env, latency_ms=80)
    r = await env.call("POST", "/runs", json={"type": "bootstrap", "model_endpoint_id": ep["id"], "dataset_id": s["dataset"]["id"], "options": {"concurrency": 1}})
    rid = r["run_id"]
    for _ in range(100):
        if len(T.CountingAdapter.calls) >= 2:
            break
        await asyncio.sleep(0.05)
    await env.call("POST", f"/runs/{rid}/pause")
    await asyncio.sleep(1.2)
    n1 = len(T.CountingAdapter.calls)
    await asyncio.sleep(0.6)
    assert len(T.CountingAdapter.calls) == n1                                                 # truly paused
    assert (await env.call("GET", f"/runs/{rid}"))["status"] == "paused"
    await env.call("POST", f"/runs/{rid}/resume")
    run = await wait_run(env, rid, 120)
    assert run["status"] == "succeeded"
    await env.call("POST", f"/runs/{rid}/pause", expect=409)


async def test_worker_crash_resumes_from_stored_hypotheses(env):
    s = await setup_dataset(env, n_utts=6)
    await env.call("POST", f"/datasets/{s['dataset']['id']}/segment", json={"segmenter": {"kind": "fixed_window", "config": {"window_s": 2}}})
    await env.drain()
    total = (await env.call("GET", f"/datasets/{s['dataset']['id']}/segments?limit=1000"))["total"]
    ep = await make_ep(env, latency_ms=100)
    T.CountingAdapter.reset()
    r = await env.call("POST", "/runs", json={"type": "bootstrap", "model_endpoint_id": ep["id"], "dataset_id": s["dataset"]["id"], "options": {"concurrency": 2}})
    for _ in range(200):
        if len(T.CountingAdapter.calls) >= 6:
            break
        await asyncio.sleep(0.05)
    await env.c.worker.stop()                                                                 # simulated process shutdown mid-run
    async with env.c.session() as db:
        job = (await db.execute(select(m.Job).where(m.Job.kind == "run"))).scalar_one()
        assert job.status == "queued"
        n = (await db.execute(select(func.count()).select_from(m.Hypothesis).where(m.Hypothesis.run_id == r["run_id"]))).scalar()
    assert 0 < n < total
    seen = dict(T.CountingAdapter.calls)
    w2 = Worker(env.c, slots=2)
    w2.start()
    try:
        await w2.drain(120)
    finally:
        await w2.stop()
    run = await env.call("GET", f"/runs/{r['run_id']}")
    assert run["status"] == "succeeded" and run["progress"]["done"] == total
    recomputed = sum(1 for sid, c in T.CountingAdapter.calls.items() if sid in seen and c > seen[sid])
    assert recomputed <= 2                                                                    # in-flight at crash time at most
