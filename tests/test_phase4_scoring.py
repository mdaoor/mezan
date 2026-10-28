import jiwer
import pytest

import tests.adapters_for_tests as T
from mizan.plugins import registry as plugins
from tests.conftest import gold_from_texts, scripted_endpoint, setup_dataset

GOLD = ["انا عندي meeting بكره", "الملف جاهز يا باشا", "we will deploy الليلة", "السلام عليكم", "send the report ارسل التقرير", ""]
A_OUT = ["انا عندي meeting بكره", "الملف جاهز يا باشا", "we will deploy الليله", "السلام عليكم", "send the report ارسل التقرير", ""]   # perfect after normalization
B_OUT = ["انا عند meting بكره", "الملف جاهز", "we will deploi الليلة", "السلام", "send report ارسل التقرير", "thanks for watching everyone"]


@pytest.fixture(autouse=True)
def _reset():
    T.CountingAdapter.reset()


async def prepared(env):
    s = await setup_dataset(env, n_utts=6)
    assert len(s["segments"]) == 6
    g = await gold_from_texts(env, s, GOLD, flags={5: ["no_speech"]})
    return s, g


async def run_eval(env, ep, g, **kw):
    r = await env.call("POST", "/runs", json={"type": "evaluation", "model_endpoint_id": ep["id"], "reference": {"type": "gold_set", "id": g["id"]},
                                              "metric_set": kw.pop("metric_set", "arabic_codeswitch_default"), "normalizer": kw.pop("normalizer", {"kind": "arabic_v1"}),
                                              "options": {"concurrency": 3, **kw.pop("options", {})}, **kw})
    await env.drain()
    return await env.call("GET", f"/runs/{r['run_id']}")


def expected_wer(refs, hyps):
    """Independent oracle: jiwer per segment, summed corpus-level; hallucinated words on empty refs count as insertions."""
    norm = plugins.normalizers.get("arabic_v1")()
    S = D = I = N = 0
    for r, h in zip(refs, hyps):
        r, h = norm.normalize(r), norm.normalize(h)
        if not r:
            I += len(h.split())
        elif not h:
            D += len(r.split()); N += len(r.split())
        else:
            o = jiwer.process_words(r, h)
            S, D, I, N = S + o.substitutions, D + o.deletions, I + o.insertions, N + len(r.split())
    return (S + D + I) / N


async def test_eval_run_matches_independent_jiwer_and_strata(env):
    s, g = await prepared(env)
    A = await scripted_endpoint(env, "A", s, A_OUT)
    B = await scripted_endpoint(env, "B", s, B_OUT)
    ra, rb = await run_eval(env, A, g), await run_eval(env, B, g)
    assert ra["status"] == rb["status"] == "succeeded"
    assert ra["summary"]["wer"] == 0.0 and ra["summary"]["cer"] == 0.0 and ra["summary"]["en_preservation"] == 1.0
    # B: corpus-level WER equals an independent jiwer computation over normalized text (non-empty refs)
    assert rb["summary"]["wer"] == pytest.approx(expected_wer(GOLD, B_OUT))
    assert rb["summary"]["wer_ar"] > 0 and rb["summary"]["wer_en"] > 0 and rb["summary"]["en_preservation"] < 1
    assert rb["summary"]["hallucination_rate"] == 1.0 and ra["summary"]["hallucination_rate"] == 0.0     # no_speech segment
    assert rb["summary"]["latency_ms"] > 0 and rb["summary"]["latency_ms_p95"] >= rb["summary"]["latency_ms_p50"]
    assert rb["summary"]["rtfx_observed"] > 0 and rb["summary"]["segments_ok"] == 6
    sc = await env.call("GET", f"/runs/{rb['id']}/scores?stratum=lang_mix&metric=wer")
    vals = {r["stratum_value"]: r for r in sc["rows"]}
    assert set(vals) >= {"mixed", "ar"} and sum(r["n"] for r in vals.values()) >= 5
    dur = await env.call("GET", f"/runs/{rb['id']}/scores?stratum=duration_bucket")
    assert {r["stratum_value"] for r in dur["rows"]} <= {"<5s", "5-15s", "15-30s", ">30s"}
    one = await env.call("GET", f"/runs/{rb['id']}/scores?stratum=lang_mix=ar&metric=wer")
    assert len(one["rows"]) == 1 and one["rows"][0]["stratum_value"] == "ar"
    # per-segment details + worst-first ordering
    worst = (await env.call("GET", f"/runs/{rb['id']}/segment-scores?metric=wer&limit=3"))["items"]
    assert worst[0]["value"] >= worst[1]["value"] >= worst[2]["value"] and worst[0]["details"]["N"] >= 0
    hyp = await env.call("GET", f"/runs/{rb['id']}/hypotheses")
    assert len(hyp["items"]) == 6 and all("wer" in h["scores"] or h["segment_id"] for h in hyp["items"])
    found = await env.call("GET", f"/runs/{rb['id']}/hypotheses?q=deploi")
    assert len(found["items"]) == 1


async def test_resolved_config_and_reproducibility(env):
    s, g = await prepared(env)
    B = await scripted_endpoint(env, "B", s, B_OUT)
    r1, r2 = await run_eval(env, B, g), await run_eval(env, B, g)
    cfg = await env.call("GET", f"/runs/{r1['id']}/resolved-config")
    assert cfg["normalizer"]["kind"] == "arabic_v1" and cfg["normalizer"]["version"] == "1.0.0" and len(cfg["normalizer"]["rules"]) >= 8
    assert cfg["reference"]["gold_set"] == "gold" and cfg["reference"]["version"] == 1 and cfg["engine"]["adapter_version"] == "9.9.9"
    assert {m_["id"] for m_ in cfg["metrics"]} >= {"wer", "cer", "wer_ar"} and cfg["metrics"][0]["version"]
    assert "api_key" not in str(cfg)
    for k in ("wer", "cer", "mer", "wer_ar", "wer_en", "en_preservation", "hallucination_rate"):
        assert r1["summary"][k] == r2["summary"][k]                                  # N4: same config, same metrics


async def test_normalizer_choice_changes_metrics_and_is_recorded(env):
    s, g = await prepared(env)
    diac = ["انَا عندي meeting بكرة", *GOLD[1:5], ""]
    D = await scripted_endpoint(env, "D", s, diac[:5] + [""])
    arab = await run_eval(env, D, g)
    none = await run_eval(env, D, g, normalizer={"kind": "none"})
    assert arab["summary"]["wer"] == 0.0 and none["summary"]["wer"] > 0
    assert none["normalizer"]["kind"] == "none" and arab["normalizer"]["kind"] == "arabic_v1"


async def test_segment_filter_and_metric_set_validation(env):
    s, g = await prepared(env)
    A = await scripted_endpoint(env, "A", s, A_OUT)
    r = await run_eval(env, A, g, options={"segment_filter": {"lang_mix": ["mixed"]}})
    assert r["progress"]["total"] == 3 and r["status"] == "succeeded"
    await env.call("POST", "/runs", expect=422, json={"type": "evaluation", "model_endpoint_id": A["id"], "reference": {"type": "gold_set", "id": g["id"]}, "metric_set": ["wer", "nope"]})
    await env.call("POST", "/runs", expect=422, json={"type": "evaluation", "model_endpoint_id": A["id"], "reference": {"type": "gold_set", "id": g["id"]}, "metric_set": "nope"})
    small = await run_eval(env, A, g, metric_set=["wer"])
    assert set(small["summary"]) - {"audio_s", "elapsed_s", "rtfx_observed", "segments_ok", "segments_failed"} == {"wer"}


async def test_compare_best_per_column_delta_and_strata(env):
    s, g = await prepared(env)
    A, B = await scripted_endpoint(env, "A", s, A_OUT), await scripted_endpoint(env, "B", s, B_OUT)
    ra, rb = await run_eval(env, A, g), await run_eval(env, B, g)
    c = await env.call("GET", f"/runs/compare?ids={rb['id']},{ra['id']}&stratum=lang_mix")
    assert c["baseline"] == rb["id"] and c["overall"]["best"]["wer"] == ra["id"] and c["overall"]["best"]["en_preservation"] == ra["id"]
    assert c["overall"]["delta_vs_baseline"]["wer"][ra["id"]] == pytest.approx(ra["summary"]["wer"] - rb["summary"]["wer"])
    assert c["lower_is_better"]["wer"] is True and c["lower_is_better"]["en_preservation"] is False
    assert set(c["stratum"]["values"]) >= {"mixed", "ar"} and c["stratum"]["values"]["mixed"]["best"]["wer"] == ra["id"]
    assert c["labels"][ra["id"]] == "accuracy vs gold"
    only = await env.call("GET", f"/runs/compare?ids={ra['id']},{rb['id']}&stratum=lang_mix=mixed")
    assert list(only["stratum"]["values"]) == ["mixed"]


async def test_disagreement_explorer_and_send_to_queue(env):
    s, g = await prepared(env)
    A, B = await scripted_endpoint(env, "A", s, A_OUT), await scripted_endpoint(env, "B", s, B_OUT)
    ra, rb = await run_eval(env, A, g), await run_eval(env, B, g)
    d = await env.call("GET", f"/runs/disagreements?ids={ra['id']},{rb['id']}&metric=wer&top=3")
    assert d["diff_base"] == "reference" and len(d["items"]) == 3
    dist = [i["distance"] for i in d["items"]]
    assert dist == sorted(dist, reverse=True) and dist[0] > 0
    top = d["items"][0]
    assert top["reference"] in GOLD and {h["run_id"] for h in top["hypotheses"]} == {ra["id"], rb["id"]}
    hb = next(h for h in top["hypotheses"] if h["run_id"] == rb["id"])
    assert any(x["op"] != "equal" for x in hb["diff"])
    ha = next(h for h in top["hypotheses"] if h["run_id"] == ra["id"])
    assert all(x["op"] == "equal" for x in ha["diff"]) or ha["text"] != top["reference"]
    assert (await env.call("GET", f"/runs/disagreements?ids={ra['id']},{rb['id']}&metric=cer&top=2"))["metric"] == "cer"
    await env.call("GET", f"/runs/disagreements?ids={ra['id']}", expect=422)
    q = await env.call("POST", f"/segments/{top['segment_id']}/queue", json={"priority": 3})
    assert q["priority"] == 3
    seg = (await env.call("GET", f"/datasets/{s['dataset']['id']}/segments?status=draft"))["items"]
    assert any(x["id"] == top["segment_id"] for x in seg)                                  # reopened as a draft for re-annotation


async def test_agreement_mode_never_called_accuracy(env):
    s, g = await prepared(env)
    A, B = await scripted_endpoint(env, "A", s, A_OUT), await scripted_endpoint(env, "B", s, B_OUT)
    ra = await run_eval(env, A, g)
    r = await env.call("POST", "/runs", json={"type": "evaluation", "model_endpoint_id": B["id"], "reference": {"type": "run", "id": ra["id"]},
                                              "dataset_id": s["dataset"]["id"], "options": {"concurrency": 2}})
    await env.drain()
    ag = await env.call("GET", f"/runs/{r['run_id']}")
    assert ag["type"] == "agreement" and ag["status"] == "succeeded" and ag["reference"] == {"type": "run", "id": ra["id"]}
    assert ag["summary"]["wer"] > 0
    c = await env.call("GET", f"/runs/compare?ids={ra['id']},{ag['id']}")
    assert c["labels"][ag["id"]] == "disagreement vs reference run"
    cfg = await env.call("GET", f"/runs/{ag['id']}/resolved-config")
    assert cfg["reference"]["run"] == ra["name"]
    # reference run's empty-hypothesis segment still participates; only successful hypotheses are used as references
    assert ag["progress"]["total"] == 6


async def test_run_list_and_filters(env):
    s, g = await prepared(env)
    A = await scripted_endpoint(env, "A", s, A_OUT)
    ra = await run_eval(env, A, g)
    lst = await env.call("GET", f"/runs?endpoint_id={A['id']}&status=succeeded&type=evaluation")
    assert [r["id"] for r in lst["items"]] == [ra["id"]] and lst["items"][0]["summary"]["wer"] == 0.0
    assert (await env.call("GET", "/runs?status=failed"))["items"] == []
    jl = await env.call("GET", f"/runs/{ra['id']}/hypotheses/export")
    lines = jl.text.strip().splitlines()
    assert len(lines) == 6 and all('"segment_id"' in ln for ln in lines)
