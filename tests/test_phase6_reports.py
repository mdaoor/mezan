import csv
import io
import json

import pytest

import tests.adapters_for_tests as T
from mizan.core.services import reports as rsvc
from tests.conftest import gold_from_texts, scripted_endpoint, setup_dataset
from tests.test_phase4_scoring import A_OUT, B_OUT, GOLD, run_eval

SECTIONS = ["Recommendation", "Setup", "Functional results", "Code-switching focus", "Robustness",
            "Non-functional results", "Disagreement highlights", "Decision matrix", "Reproduce", "Appendix"]


async def full_campaign(env, load_for=("A", "B"), extra_ep=None):
    s = await setup_dataset(env, n_utts=6)
    g = await gold_from_texts(env, s, GOLD, flags={5: ["no_speech"]}, name="gold-q4")
    A = await scripted_endpoint(env, "Model A", s, A_OUT, hardware="1x H200", latency_ms=10)
    B = await scripted_endpoint(env, "Model B", s, B_OUT, latency_ms=10)
    ra, rb = await run_eval(env, A, g), await run_eval(env, B, g)
    sc = await env.call("POST", "/load-scenarios", json={"name": "tiny", "concurrency_steps": [1, 4], "step_duration_s": 0.6, "warmup_s": 0.2,
                                                         "timeout_s": 5, "length_buckets": {"<5s": 1.0}})
    lids = []
    for name, ep in (("A", A), ("B", B)):
        if name in load_for:
            r = await env.call("POST", "/load-runs", json={"model_endpoint_id": ep["id"], "scenario_id": sc["id"], "dataset_id": s["dataset"]["id"]})
            lids.append(r["load_run_id"])
    await env.drain(60)
    return s, g, A, B, ra, rb, lids


async def make_report(env, s, run_ids, lids, options=None, name="Replace Whisper, Q4"):
    cp = await env.call("POST", "/campaigns", json={"project_id": s["project"]["id"], "name": name, "question": "Replace Whisper for Arabic+English?",
                                                    "run_ids": run_ids, "load_run_ids": lids})
    r = await env.call("POST", f"/campaigns/{cp['id']}/reports", json={"template": "decision_v1", "options": options or {}})
    await env.drain(60)
    return cp, await env.call("GET", f"/reports/{r['report_id']}")


async def test_decision_v1_report_structure_and_content(env):
    s, g, A, B, ra, rb, lids = await full_campaign(env)
    cp, rep = await make_report(env, s, [ra["id"], rb["id"]], lids)
    assert rep["status"] == "ready" and rep["version"] == 1 and rep["template"] == "decision_v1"
    assert [x["title"] for x in rep["sections"]] == SECTIONS
    assert rep["title"] == "Replace Whisper, Q4" and rep["header"][0].startswith("**Gold set:** gold-q4 v1")
    sec = {x["id"]: x for x in rep["sections"]}
    tbl = next(b for b in sec["functional"]["blocks"] if b["type"] == "table")
    assert tbl["columns"][:3] == ["Endpoint", "WER", "WER 95% CI"]
    rows = {r[0]: r for r in tbl["rows"]}
    assert rows["Model A"][1] == 0.0 and rows["Model B"][1] > 0.4
    assert "–" in rows["Model B"][2]                                                     # CI "lo–hi"
    assert any(b["type"] == "chart" and b["title"] == "WER by endpoint" for b in sec["functional"]["blocks"])
    assert any(b.get("type") == "heading" and b["text"] == "By lang mix" for b in sec["functional"]["blocks"])
    setup = sec["setup"]["blocks"][0]
    by_name = {r[0]: r for r in setup["rows"]}
    assert set(by_name) == {"Model A", "Model B"} and by_name["Model A"][3] == "1x H200" and by_name["Model B"][3] == "—"
    assert len(by_name["Model A"][4]) == 10 and by_name["Model A"][4] != by_name["Model B"][4]            # config hash
    assert any("tatweel" in str(b) for b in sec["setup"]["blocks"])                         # normalizer rules printed
    dm = sec["decision"]["blocks"][0]
    assert dm["columns"][1] == "Accuracy (50%)" and dm["columns"][-1] == "Score"
    assert dm["rows"][0][0] == "Model A" and rep["decision"]["ranking"][0]["score"] > rep["decision"]["ranking"][1]["score"]
    assert "Recommended endpoint: Model A" in rep["summary_text"]
    cs = sec["code_switching"]["blocks"]
    assert any(b["type"] == "table" and b["columns"][:2] == ["#", "Reference"] for b in cs)
    rb_ = sec["robustness"]["blocks"][0]
    rob = {r[0]: r for r in rb_["rows"]}
    assert rob["Model B"][1] == 1.0 and rob["Model A"][1] == 0.0                              # B hallucinated on the no_speech segment
    dis = sec["disagreements"]["blocks"][0]
    assert dis["columns"][:4] == ["#", "Segment", "Distance", "Reference"] and len(dis["rows"]) >= 1
    assert "mizan run eval --endpoint" in sec["reproduce"]["blocks"][0]["text"]
    assert sec["appendix"]["blocks"][0]["type"] == "table"


async def test_report_with_load_data_for_same_endpoints(env):
    """Evaluation and load runs reference the same ModelEndpoint, so throughput joins the decision matrix."""
    s = await setup_dataset(env, n_utts=6)
    g = await gold_from_texts(env, s, GOLD, flags={5: ["no_speech"]})
    A = await scripted_endpoint(env, "Slow Accurate", s, A_OUT, kind="counting", latency_ms=5)
    # a mock endpoint serves both runs: scripted text + simulated latency
    tr_a = {seg["id"]: t for seg, t in zip(s["segments"], A_OUT)}
    tr_b = {seg["id"]: t for seg, t in zip(s["segments"], B_OUT)}
    fast = await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "fast", "display_name": "Fast Sloppy",
                                                            "config": {"transcripts": tr_b, "latency_ms": 5, "max_concurrency": 8}})
    slow = await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "slow", "display_name": "Slow Accurate",
                                                            "config": {"transcripts": tr_a, "latency_ms": 60, "saturate_at": 1, "max_concurrency": 8}})
    ra, rb = await run_eval(env, slow, g), await run_eval(env, fast, g)
    sc = await env.call("POST", "/load-scenarios", json={"name": "t", "concurrency_steps": [1, 4], "step_duration_s": 0.8, "warmup_s": 0.2, "timeout_s": 5,
                                                         "length_buckets": {"<5s": 1.0}})
    lids = []
    for ep in (slow, fast):
        lids.append((await env.call("POST", "/load-runs", json={"model_endpoint_id": ep["id"], "scenario_id": sc["id"], "dataset_id": s["dataset"]["id"]}))["load_run_id"])
    await env.drain(60)
    cp, rep = await make_report(env, s, [ra["id"], rb["id"]], lids)
    sec = {x["id"]: x for x in rep["sections"]}
    comps = rep["decision"]["components"]
    assert comps[slow["id"]]["throughput"] < comps[fast["id"]]["throughput"] == 1.0           # normalized to the best endpoint
    assert comps[slow["id"]]["accuracy"] == 1.0 and comps[fast["id"]]["accuracy"] < 0.6
    charts = [b for b in sec["non_functional"]["blocks"] if b["type"] == "chart"]
    assert [c["title"] for c in charts] == ["p95 latency vs concurrency", "RTFx vs concurrency"]
    assert {s_["name"] for s_ in charts[0]["series"]} == {"Slow Accurate", "Fast Sloppy"} and len(charts[0]["series"][0]["points"]) == 2
    nf = next(b for b in sec["non_functional"]["blocks"] if b["type"] == "table")
    assert nf["columns"][:4] == ["Endpoint", "Concurrency", "p50 (ms)", "p95 (ms)"] and len(nf["rows"]) == 4
    assert any("saturates at concurrency 4" in i for b in sec["non_functional"]["blocks"] if b["type"] == "bullets" for i in b["items"])
    # weighting accuracy at 100% => Slow Accurate; throughput at 100% => Fast Sloppy; re-rank is persisted on the report
    top = lambda r: r["decision"]["ranking"][0]["endpoint_id"]  # noqa: E731
    w1 = await env.call("PATCH", f"/reports/{rep['id']}", json={"weights": {"accuracy": 1, "code_switch": 0, "throughput": 0, "robustness": 0}})
    assert top(w1) == slow["id"] and "Recommended endpoint: Slow Accurate" in w1["summary_text"]
    w2 = await env.call("PATCH", f"/reports/{rep['id']}", json={"weights": {"accuracy": 0, "code_switch": 0, "throughput": 1, "robustness": 0}})
    assert top(w2) == fast["id"] and "Recommended endpoint: Fast Sloppy" in w2["summary_text"]
    assert top(await env.call("GET", f"/reports/{rep['id']}")) == fast["id"]                   # persisted
    await env.call("PATCH", f"/reports/{rep['id']}", expect=422, json={"weights": {"accuracy": 0, "code_switch": 0, "throughput": 0, "robustness": 0}})
    # an edited executive summary survives later re-weighting
    await env.call("PATCH", f"/reports/{rep['id']}", json={"summary_text": "Our call: Fast Sloppy for batch."})
    after = await env.call("PATCH", f"/reports/{rep['id']}", json={"weights": {"accuracy": 1, "code_switch": 0, "throughput": 0, "robustness": 0}})
    assert after["summary_text"] == "Our call: Fast Sloppy for batch." and top(after) == slow["id"]


async def test_exports_markdown_html_json_csv(env):
    s, g, A, B, ra, rb, lids = await full_campaign(env)
    cp, rep = await make_report(env, s, [ra["id"], rb["id"]], lids)
    md = (await env.call("GET", f"/reports/{rep['id']}/export?format=md")).text
    assert md.startswith("# Replace Whisper, Q4\n") and "**Question:** Replace Whisper for Arabic+English?" in md
    assert "**Gold set:** gold-q4 v1" in md and "## 1. Recommendation" in md and "## 8. Decision matrix" in md
    assert "| Endpoint | WER | WER 95% CI | CER | MER | WER-AR | WER-EN | EN-preservation | Halluc. | p95 latency (ms) |" in md
    assert "|---|---|---|" in md and "Model A" in md and "انا عندي meeting بكره" in md
    assert "\r" not in md and "<script" not in md
    html = (await env.call("GET", f"/reports/{rep['id']}/export?format=html")).text
    assert "<svg" in html and 'dir=auto' in html and "unicode-bidi:plaintext" in html and "prefers-color-scheme:dark" in html
    js = json.loads((await env.client.get(f"/api/v1/reports/{rep['id']}/export?format=json", headers=env.auth())).text)
    assert [x["title"] for x in js["sections"]] == SECTIONS
    cv = (await env.call("GET", f"/reports/{rep['id']}/export?format=csv")).content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(cv)))
    i = next(k for k, r in enumerate(rows) if r and r[0].startswith("# 3. Functional results"))
    assert rows[i + 1][:3] == ["Endpoint", "WER", "WER 95% CI"] and len(rows[i + 2]) == len(rows[i + 1])
    again = await env.call("GET", f"/reports/{rep['id']}")
    assert {a["format"] for a in again["artifacts"]} == {"md", "html", "json", "csv"}
    await env.call("GET", f"/reports/{rep['id']}/export?format=pdf", expect=422)


async def test_report_is_deterministic_and_versioned(env):
    s, g, A, B, ra, rb, lids = await full_campaign(env, load_for=())
    cp, r1 = await make_report(env, s, [ra["id"], rb["id"]], [])
    r2id = (await env.call("POST", f"/campaigns/{cp['id']}/reports", json={}))["report_id"]
    await env.drain()
    r2 = await env.call("GET", f"/reports/{r2id}")
    assert (r1["version"], r2["version"]) == (1, 2)
    ci = lambda r: [b for b in r["sections"][2]["blocks"] if b["type"] == "table"][0]["rows"]  # noqa: E731
    assert ci(r1) == ci(r2)                                                                   # seeded bootstrap => identical CIs
    assert "Throughput" in r1["decision"]["ranking"][0]["missing"][0].title() or r1["decision"]["ranking"][0]["missing"] == ["throughput"]
    assert "no evidence for throughput" in r1["summary_text"]
    lst = await env.call("GET", f"/campaigns/{cp['id']}/reports")
    assert [x["version"] for x in lst["items"]] == [2, 1]
    nf = next(x for x in r1["sections"] if x["id"] == "non_functional")
    assert nf["blocks"][0]["text"].startswith("No succeeded load runs")


async def test_agreement_runs_are_labelled_and_excluded_from_accuracy(env):
    s = await setup_dataset(env, n_utts=6)
    g = await gold_from_texts(env, s, GOLD, flags={5: ["no_speech"]})
    A, B = await scripted_endpoint(env, "A", s, A_OUT), await scripted_endpoint(env, "B", s, B_OUT)
    ra = await run_eval(env, A, g)
    r = await env.call("POST", "/runs", json={"type": "evaluation", "model_endpoint_id": B["id"], "reference": {"type": "run", "id": ra["id"]}, "dataset_id": s["dataset"]["id"]})
    await env.drain()
    cp, rep = await make_report(env, s, [r["run_id"]], [])
    func = next(b for b in next(x for x in rep["sections"] if x["id"] == "functional")["blocks"] if b["type"] == "table")
    assert "agreement" in func["title"] and func["rows"][0][0].endswith("(agreement)")
    assert rep["decision"]["components"][B["id"]]["accuracy"] is None                        # never called accuracy
    assert any("never as accuracy" in str(x) for x in rep["sections"][0]["blocks"])


async def test_unsucceeded_runs_excluded_and_bootstrap_rejected(env):
    s = await setup_dataset(env, n_utts=3)
    ep = await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "m", "display_name": "m", "config": {"latency_ms": 1}})
    b = await env.call("POST", f"/datasets/{s['dataset']['id']}/bootstrap", json={"model_endpoint_id": ep["id"]})
    await env.drain()
    await env.call("POST", "/campaigns", expect=422, json={"project_id": s["project"]["id"], "name": "x", "run_ids": [b["run_id"]]})
    cp = await env.call("POST", "/campaigns", json={"project_id": s["project"]["id"], "name": "empty"})
    await env.call("POST", f"/campaigns/{cp['id']}/reports", json={"template": "nope"}, expect=422)
    r = await env.call("POST", f"/campaigns/{cp['id']}/reports", json={})
    await env.drain()
    rep = await env.call("GET", f"/reports/{r['report_id']}")
    assert rep["status"] == "ready" and "not yet enough evidence" in rep["summary_text"]
    await env.call("PATCH", f"/campaigns/{cp['id']}", json={"status": "closed", "add_run_ids": [], "name": "renamed"})
    assert (await env.call("GET", f"/campaigns/{cp['id']}"))["name"] == "renamed"


def test_rank_and_weights_pure_functions():
    comps = {"a": {"accuracy": 0.9, "code_switch": None, "throughput": 0.5, "robustness": 1.0},
             "b": {"accuracy": 0.8, "code_switch": 0.8, "throughput": 1.0, "robustness": 0.9}}
    w = rsvc.normalize_weights({"accuracy": 50, "code_switch": 20, "throughput": 20, "robustness": 10})
    assert w == pytest.approx({"accuracy": .5, "code_switch": .2, "throughput": .2, "robustness": .1})
    r = rsvc.rank(comps, w)
    sa = (0.9 * .5 + 0.5 * .2 + 1.0 * .1) / .8                                                # missing component renormalized
    assert next(x for x in r if x["endpoint_id"] == "a")["score"] == pytest.approx(sa)
    assert next(x for x in r if x["endpoint_id"] == "a")["missing"] == ["code_switch"]
    with pytest.raises(Exception):
        rsvc.normalize_weights({"accuracy": -1})
