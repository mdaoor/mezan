"""Handover §11.10: upload one Arabic/English file -> segment -> bootstrap with endpoint A -> annotate 10 segments ->
freeze gold v1 -> run A and B -> compare -> load-test A -> generate campaign report -> export Markdown."""
import pytest

from tests.conftest import setup_dataset

TRUTH = ["مرحبا بكم في الاجتماع", "نبدأ بمراجعة the quarterly report", "الميزانية مناسبة جدا", "please send the deck قبل الغداء",
         "هل وصلتك الرسالة", "we need more GPU memory اليوم", "شكرا جزيلا", "الخادم متوقف عن العمل", "restart the service من فضلك", "انتهى الاجتماع"]


async def test_full_handover_demo_flow(env):
    s = await setup_dataset(env, n_utts=12)
    did, pid = s["dataset"]["id"], s["project"]["id"]
    segs = s["segments"]
    assert len(segs) == 12

    # endpoint A: a decent model whose drafts contain errors; endpoint B: a worse model
    draft_a = {sg["id"]: (TRUTH[i].replace("الاجتماع", "الاجتماع") if i % 3 else TRUTH[i].replace("the ", "")) for i, sg in enumerate(segs[:10])}
    draft_b = {sg["id"]: " ".join(TRUTH[i].split()[:-1]) or "x" for i, sg in enumerate(segs[:10])}
    A = await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "model-a", "display_name": "Model A (mock vLLM GPU0)",
                                                         "config": {"transcripts": draft_a, "default_text": "", "latency_ms": 15, "max_concurrency": 16, "hardware": "H200 #0"}})
    B = await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "model-b", "display_name": "Model B (mock vLLM GPU1)",
                                                         "config": {"transcripts": draft_b, "default_text": "", "latency_ms": 10, "max_concurrency": 16, "hardware": "H200 #1"}})
    test = await env.call("POST", f"/model-endpoints/{A['id']}/test")
    assert test["ok"]

    # bootstrap drafts with A
    bs = await env.call("POST", f"/datasets/{did}/bootstrap", json={"model_endpoint_id": A["id"], "options": {"concurrency": 8, "language_hint": "ar"}})
    await env.drain()
    assert (await env.call("GET", f"/runs/{bs['run_id']}"))["status"] == "succeeded"

    # team: annotator corrects drafts, reviewer approves
    at, _ = await env.make_user("Contributor", "annotator")
    rt, _ = await env.make_user("Reviewer", "reviewer")
    claim = await env.call("POST", f"/datasets/{did}/claims", token=at, json={"minutes": 1})
    mine = {x["id"]: x for x in claim["segments"]}
    for i, sg in enumerate(segs[:10]):
        assert sg["id"] in mine
        work = await env.call("GET", f"/segments/{sg['id']}/annotation", token=at)
        assert work["annotation"]["source"].startswith("machine:")                      # edit, don't type from scratch
        await env.call("PUT", f"/segments/{sg['id']}/annotation", token=at, json={"text": TRUTH[i]})
        await env.call("POST", f"/segments/{sg['id']}/annotation/submit", token=at)
        await env.call("POST", f"/segments/{sg['id']}/annotation/review", token=rt, json={"action": "approve"})
    for sg in segs[10:]:
        await env.call("PATCH", f"/segments/{sg['id']}", json={"flags": ["unintelligible"]})
        if sg["id"] in mine:
            await env.call("DELETE", f"/claims/{sg['id']}", token=at)

    gold = await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": "demo-gold"})
    assert gold["version"] == 1 and gold["segment_count"] == 10

    # evaluate A and B against gold v1 (one request creates N runs)
    batch = await env.call("POST", "/runs/batch", json={"model_endpoint_ids": [A["id"], B["id"]], "run": {
        "type": "evaluation", "model_endpoint_id": A["id"], "reference": {"type": "gold_set", "id": gold["id"]},
        "metric_set": "arabic_codeswitch_default", "normalizer": {"kind": "arabic_v1"}, "options": {"concurrency": 8}}})
    await env.drain()
    ra, rb = [r["run_id"] for r in batch["runs"]]
    runs = [await env.call("GET", f"/runs/{r}") for r in (ra, rb)]
    assert all(r["status"] == "succeeded" for r in runs)
    wer_a, wer_b = runs[0]["summary"]["wer"], runs[1]["summary"]["wer"]
    assert wer_a < wer_b and wer_b > 0.05

    cmp_ = await env.call("GET", f"/runs/compare?ids={ra},{rb}&stratum=duration_bucket")
    assert cmp_["overall"]["best"]["wer"] == ra

    # load-test A, the same endpoint that was evaluated
    sc = await env.call("POST", "/load-scenarios", json={"name": "demo-ramp", "concurrency_steps": [1, 2, 4], "step_duration_s": 0.7, "warmup_s": 0.2,
                                                         "timeout_s": 5, "length_buckets": {"<5s": 0.5, "5-15s": 0.5}})
    lr = await env.call("POST", "/load-runs", json={"model_endpoint_id": A["id"], "scenario_id": sc["id"], "dataset_id": did})
    await env.drain(60)
    load = await env.call("GET", f"/load-runs/{lr['load_run_id']}")
    assert load["status"] == "succeeded" and len(load["steps"]) == 3

    # campaign report -> markdown
    cp = await env.call("POST", "/campaigns", json={"project_id": pid, "name": "Replace Whisper, Q4", "question": "Which endpoint should replace Whisper?",
                                                    "run_ids": [ra, rb], "load_run_ids": [lr["load_run_id"]]})
    rep = await env.call("POST", f"/campaigns/{cp['id']}/reports", json={"template": "decision_v1"})
    await env.drain()
    md = (await env.call("GET", f"/reports/{rep['report_id']}/export?format=md")).text
    assert md.startswith("# Replace Whisper, Q4") and "demo-gold v1 (" in md
    for heading in ("## 1. Recommendation", "## 3. Functional results", "## 6. Non-functional results", "## 8. Decision matrix", "## 9. Reproduce"):
        assert heading in md
    assert "Recommended endpoint: Model A" in md and "| Model A (mock vLLM GPU0) |" in md
    assert "Concurrency" in md and "saturat" in md.lower() or "no saturation" in md.lower()
    # audit trail captured the whole journey
    actions = {a["action"] for a in (await env.call("GET", "/audit?limit=500"))["items"]}
    assert {"run.create", "goldset.freeze", "annotation.approve", "loadrun.create", "campaign.create", "claims.create"} <= actions
