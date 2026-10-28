import asyncio
from datetime import datetime, timezone

import pytest

from mizan.core import models as m
from mizan.plugins import registry as plugins
from mizan.plugins.base import TelemetrySampleData, TelemetrySource
from mizan.workers.load import bucket_range, saturation_point, summarize_step
from tests.conftest import setup_dataset


class FakeGPU(TelemetrySource):
    kind = "fake_gpu"
    n = 0

    async def sample(self):
        FakeGPU.n += 1
        return [TelemetrySampleData(datetime.now(timezone.utc), "gpu0", 50.0 + FakeGPU.n, 1000.0 * FakeGPU.n, 60.0, 300.0)]


plugins.telemetry_sources.register(FakeGPU)


def test_bucket_labels():
    assert bucket_range("5-15s") == (5.0, 15.0) and bucket_range("<5s") == (0.0, 5.0)
    assert bucket_range(">30s") == (30.0, float("inf")) and bucket_range("15-30s") == (15.0, 30.0)


def test_saturation_rules():
    steps = [dict(step=0, concurrency=1, p95=100, error_rate=0), dict(step=1, concurrency=4, p95=150, error_rate=0),
             dict(step=2, concurrency=8, p95=400, error_rate=0)]
    assert saturation_point(steps) == {"step": 2, "concurrency": 8, "reason": "p95_growth"}          # >2x previous
    steps[2]["p95"] = 300                                                                              # exactly 2x is not "more than"
    assert saturation_point(steps) is None
    steps[1]["error_rate"] = 0.02
    assert saturation_point(steps) == {"step": 1, "concurrency": 4, "reason": "error_rate"}
    assert saturation_point([]) is None


def test_summarize_step_math():
    samples = [{"ok": True, "latency_ms": float(v), "error_class": None, "audio_s": 10.0} for v in range(1, 101)]
    samples += [{"ok": False, "latency_ms": 5.0, "error_class": "timeout", "audio_s": 10.0}] * 4
    s = summarize_step(0, 8, samples, 10.0)
    assert s["p50"] == pytest.approx(50.5) and s["p95"] == pytest.approx(95.05) and s["p99"] == pytest.approx(99.01)
    assert s["rtfx"] == pytest.approx(100.0) and s["req_per_s"] == pytest.approx(10.4) and s["timeouts"] == 4
    assert s["error_rate"] == pytest.approx(4 / 104) and s["error_classes"] == {"timeout": 4}


async def mock_ep(env, **cfg):
    return await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "m", "display_name": cfg.pop("name", "mock"),
                                                            "config": {"latency_ms": 20, **cfg}})


async def scenario(env, **kw):
    body = {"name": "tiny", "concurrency_steps": [1, 4], "step_duration_s": 0.8, "warmup_s": 0.3, "timeout_s": 5,
            "length_buckets": {"<5s": 0.5, "5-15s": 0.5}, **kw}
    return await env.call("POST", "/load-scenarios", json=body)


async def start(env, ep, sc, s, tele=None):
    r = await env.call("POST", "/load-runs", json={"model_endpoint_id": ep["id"], "scenario_id": sc["id"], "dataset_id": s["dataset"]["id"],
                                                   **({"telemetry_source": tele} if tele else {})})
    await env.drain(60)
    return await env.call("GET", f"/load-runs/{r['load_run_id']}")


async def test_preset_scenario_seeded_and_validation(env):
    sc = (await env.call("GET", "/load-scenarios"))["items"]
    p = next(x for x in sc if x["name"] == "ramp-1-64")
    assert p["concurrency_steps"] == [1, 2, 4, 8, 16, 32, 48, 64] and p["step_duration_s"] == 120 and p["length_buckets"] == {"5-15s": 0.6, "15-30s": 0.4}
    await env.call("POST", "/load-scenarios", expect=422, json={"name": "x", "concurrency_steps": []})
    await env.call("POST", "/load-scenarios", expect=422, json={"name": "x", "concurrency_steps": [0]})
    await env.call("POST", "/load-scenarios", expect=422, json={"name": "x", "concurrency_steps": [1], "length_buckets": {"weird": 1}})


async def test_load_run_latency_percentiles_rtfx_and_saturation(env):
    s = await setup_dataset(env, n_utts=6)
    ep = await mock_ep(env, saturate_at=1, max_concurrency=16)
    sc = await scenario(env, concurrency_steps=[1, 4])
    lr = await start(env, ep, sc, s)
    assert lr["status"] == "succeeded" and [x["concurrency"] for x in lr["steps"]] == [1, 4]
    s1, s4 = lr["steps"]
    assert 15 <= s1["p50"] <= 80 and s1["requests"] >= 10 and s1["error_rate"] == 0
    assert s1["p50"] <= s1["p95"] <= s1["p99"]
    assert s4["p95"] > 2 * s1["p95"] * 0.9 and s4["req_per_s"] > 0 and s4["rtfx"] > 0
    assert s1["rtfx"] == pytest.approx(s1["req_per_s"] * 4.5 * 1.0, rel=0.5)                  # audio_s per request ~4.5 s
    assert lr["saturation"] == {"step": 1, "concurrency": 4, "reason": "p95_growth"}
    assert lr["live"]["requests"] == s1["requests"] + s4["requests"]
    samples = await env.call("GET", f"/load-runs/{lr['id']}/samples?step=1&limit=5")
    assert len(samples["items"]) == 5 and samples["next_cursor"] == "5" and all(i["step"] == 1 and i["ok"] for i in samples["items"])
    assert lr["info"]["scenario"]["effective_steps"] == [1, 4]
    lst = await env.call("GET", "/load-runs")
    assert lst["items"][0]["best_rtfx"] > 0


async def test_concurrency_is_capped_by_endpoint_max_concurrency(env):
    s = await setup_dataset(env, n_utts=4)
    ep = await mock_ep(env, max_concurrency=2)
    lr = await start(env, ep, await scenario(env, concurrency_steps=[1, 8]), s)
    assert [x["concurrency"] for x in lr["steps"]] == [1, 2] and lr["info"]["scenario"]["effective_steps"] == [1, 2]


async def test_error_guardrail_aborts_run(env):
    s = await setup_dataset(env, n_utts=4)
    ep = await mock_ep(env, error_rate=1.0, latency_ms=2)
    lr = await start(env, ep, await scenario(env, concurrency_steps=[2, 4, 8], step_duration_s=3), s)
    assert lr["status"] == "succeeded" and len(lr["steps"]) == 1                              # later steps never started
    assert lr["steps"][0]["error_rate"] == 1.0 and lr["steps"][0]["error_classes"] == {"http_503": lr["steps"][0]["requests"]}
    assert "aborted" in lr["info"]["aborted_reason"] and lr["saturation"]["reason"] == "error_rate"


async def test_timeouts_counted(env):
    s = await setup_dataset(env, n_utts=4)
    ep = await mock_ep(env, latency_ms=1500)
    lr = await start(env, ep, await scenario(env, concurrency_steps=[2], timeout_s=0.3, step_duration_s=1.0, warmup_s=0), s)
    st = lr["steps"][0]
    assert st["timeouts"] > 0 and st["error_classes"].get("timeout") == st["timeouts"]


async def test_gpu_telemetry_timeline_and_step_join(env):
    s = await setup_dataset(env, n_utts=4)
    ep = await mock_ep(env)
    FakeGPU.n = 0
    lr = await start(env, ep, await scenario(env, concurrency_steps=[1, 2], step_duration_s=1.6), s, tele="fake_gpu")
    tl = await env.call("GET", f"/load-runs/{lr['id']}/telemetry")
    assert len(tl["samples"]) >= 3 and tl["samples"][0]["device"] == "gpu0" and len(tl["steps"]) == 2
    gpu = lr["steps"][1]["gpu"]["gpu0"]
    assert gpu["util_max"] >= gpu["util_avg"] > 50 and gpu["mem_used_mb_max"] >= 1000
    assert lr["telemetry_source"] == "fake_gpu"
    await env.call("POST", "/load-runs", expect=422, json={"model_endpoint_id": ep["id"], "scenario_id": (await scenario(env))["id"],
                                                          "dataset_id": s["dataset"]["id"], "telemetry_source": "bogus"})


async def test_cancel_load_run(env):
    s = await setup_dataset(env, n_utts=4)
    ep = await mock_ep(env)
    sc = await scenario(env, concurrency_steps=[1, 2, 4], step_duration_s=4, warmup_s=0)
    r = await env.call("POST", "/load-runs", json={"model_endpoint_id": ep["id"], "scenario_id": sc["id"], "dataset_id": s["dataset"]["id"]})
    await asyncio.sleep(1.2)
    await env.call("POST", f"/load-runs/{r['load_run_id']}/cancel")
    await env.drain(30)
    lr = await env.call("GET", f"/load-runs/{r['load_run_id']}")
    assert lr["status"] == "cancelled" and len(lr["steps"]) <= 1
    await env.call("POST", f"/load-runs/{r['load_run_id']}/cancel", expect=409)


async def test_load_run_needs_segments(env):
    p = await env.call("POST", "/projects", json={"name": "P"})
    d = await env.call("POST", f"/projects/{p['id']}/datasets", json={"name": "D"})
    ep = await mock_ep(env)
    await env.call("POST", "/load-runs", expect=422, json={"model_endpoint_id": ep["id"], "scenario_id": (await scenario(env))["id"], "dataset_id": d["id"]})
