import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from mizan.core import models as m
from mizan.core.db import utcnow
from tests.conftest import setup_dataset


async def team(env):
    a1, u1 = await env.make_user("Ann", "annotator")
    a2, u2 = await env.make_user("Bob", "annotator")
    rv, ur = await env.make_user("Rae", "reviewer")
    return {"a1": a1, "a2": a2, "rv": rv, "u1": u1, "u2": u2, "ur": ur}


async def test_concurrent_claims_are_disjoint_and_time_budgeted(env):
    s = await setup_dataset(env, n_utts=12)
    did, t = s["dataset"]["id"], await team(env)
    total = sum(x["end_s"] - x["start_s"] for x in s["segments"])           # 12 x 4.5 s = 54 s
    r1, r2 = await asyncio.gather(
        env.call("POST", f"/datasets/{did}/claims", token=t["a1"], json={"minutes": 0.4}),
        env.call("POST", f"/datasets/{did}/claims", token=t["a2"], json={"minutes": 0.4}))
    ids1, ids2 = set(r1["segment_ids"]), set(r2["segment_ids"])
    assert ids1 and ids2 and not ids1 & ids2
    assert 24 <= r1["seconds"] < 24 + 4.5 and 24 <= r2["seconds"] < 24 + 4.5   # budget met even when racing
    assert len(ids1 | ids2) <= 12 and total == pytest.approx(54)
    mine = await env.call("GET", f"/datasets/{did}/claims/mine", token=t["a1"])
    assert {x["id"] for x in mine["segments"]} == ids1
    # a claimed segment cannot be edited by someone else
    sid = next(iter(ids1))
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a2"], expect=409, json={"text": "steal"})
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "mine"})


async def test_many_parallel_claimers_never_overlap(env):
    s = await setup_dataset(env, n_utts=12)
    did = s["dataset"]["id"]
    users = [(await env.make_user(f"U{i}", "annotator"))[0] for i in range(4)]
    rs = await asyncio.gather(*(env.call("POST", f"/datasets/{did}/claims", token=u, json={"minutes": 0.3, "strategy": "random"}) for u in users))
    seen: set[str] = set()
    for r in rs:
        ids = set(r["segment_ids"])
        assert not ids & seen
        seen |= ids


async def test_claim_expiry_returns_segment_with_draft_preserved(env):
    s = await setup_dataset(env, n_utts=3)
    did, t = s["dataset"]["id"], await team(env)
    r = await env.call("POST", f"/datasets/{did}/claims", token=t["a1"], json={"minutes": 1})
    sid = r["segment_ids"][0]
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "my partial draft"})
    # while claimed, bob cannot claim it
    rb = await env.call("POST", f"/datasets/{did}/claims", token=t["a2"], json={"minutes": 10})
    assert sid not in rb["segment_ids"]
    async with env.c.session() as db:   # time passes
        await db.execute(update(m.Annotation).where(m.Annotation.claimed_by == t["u1"]["id"]).values(claim_expires_at=utcnow() - timedelta(minutes=1)))
        await db.commit()
    assert sid not in {x["id"] for x in (await env.call("GET", f"/datasets/{did}/claims/mine", token=t["a1"]))["segments"]}
    rb2 = await env.call("POST", f"/datasets/{did}/claims", token=t["a2"], json={"minutes": 10})
    assert sid in rb2["segment_ids"]
    w = await env.call("GET", f"/segments/{sid}/annotation", token=t["a2"])
    assert w["annotation"]["text"] == "my partial draft" and w["annotation"]["claimed_by"] == t["u2"]["id"]
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], expect=409, json={"text": "late"})


async def test_release_and_extend(env):
    s = await setup_dataset(env, n_utts=3)
    did, t = s["dataset"]["id"], await team(env)
    sid = (await env.call("POST", f"/datasets/{did}/claims", token=t["a1"], json={"minutes": 0.1}))["segment_ids"][0]
    old = (await env.call("GET", f"/segments/{sid}/annotation", token=t["a1"]))["annotation"]["claim_expires_at"]
    ext = await env.call("POST", f"/claims/{sid}/extend?minutes=120", token=t["a1"])
    assert ext["claim_expires_at"] > old
    await env.call("POST", f"/claims/{sid}/extend", token=t["a2"], expect=409)
    await env.call("DELETE", f"/claims/{sid}", token=t["a2"])                       # anyone can release a claim
    assert sid not in {x["id"] for x in (await env.call("GET", f"/datasets/{did}/claims/mine", token=t["a1"]))["segments"]}
    again = await env.call("POST", f"/datasets/{did}/claims", token=t["a2"], json={"minutes": 0.05})
    assert again["segment_ids"][0] == sid                                          # released segment is back in the pool


async def test_versioned_saves_source_tracking_and_validation(env):
    s = await setup_dataset(env, n_utts=3)
    did, t = s["dataset"]["id"], await team(env)
    ep = await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "m", "display_name": "mock", "config": {"default_text": "مسودة draft", "latency_ms": 1}})
    await env.call("POST", f"/datasets/{did}/bootstrap", json={"model_endpoint_id": ep["id"]})
    await env.drain()
    sid = (await env.call("POST", f"/datasets/{did}/claims", token=t["a1"], json={"minutes": 0.1}))["segment_ids"][0]
    w = await env.call("GET", f"/segments/{sid}/annotation", token=t["a1"])
    assert w["annotation"]["source"] == f"machine:{ep['id']}" and w["machine_draft"]["text"] == "مسودة draft"
    v_same = await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "مسودة draft"})
    assert v_same["source"].startswith("machine:") and v_same["version"] == w["annotation"]["version"] + 1     # unchanged text keeps machine source
    v_edit = await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "مسودة meeting"})
    assert v_edit["source"] == "human" and v_edit["version"] == v_same["version"] + 1
    assert [h["version"] for h in (await env.call("GET", f"/segments/{sid}/annotation", token=t["a1"]))["history"]] == [3, 2, 1]
    nfc = await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "é"})            # NFC enforced
    assert nfc["text"] == "é"
    sp = await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "انا meeting", "spans": [{"start_char": 4, "end_char": 11, "lang": "en"}]})
    assert sp["spans"] == [{"start_char": 4, "end_char": 11, "lang": "en"}]
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], expect=422, json={"text": "ab", "spans": [{"start_char": 0, "end_char": 9, "lang": "en"}]})
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], expect=422, json={"text": "ab", "flags": ["weird"]})
    auto = await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "انا عندي meeting"})
    assert [x["lang"] for x in auto["spans"]] == ["ar", "en"]                                                    # script-detected
    vt, _ = await env.make_user("Vee", "viewer")
    await env.call("PUT", f"/segments/{sid}/annotation", token=vt, expect=403, json={"text": "x"})


async def test_submit_rules_and_flags(env):
    s = await setup_dataset(env, n_utts=3)
    did, t = s["dataset"]["id"], await team(env)
    sid = (await env.call("POST", f"/datasets/{did}/claims", token=t["a1"], json={"minutes": 0.1}))["segment_ids"][0]
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": ""})
    await env.call("POST", f"/segments/{sid}/annotation/submit", token=t["a1"], expect=422)               # empty without a flag
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "", "flags": ["no_speech"]})
    sub = await env.call("POST", f"/segments/{sid}/annotation/submit", token=t["a1"])
    assert sub["status"] == "submitted" and sub["claimed_by"] is None
    seg = (await env.call("GET", f"/datasets/{did}/segments?status=submitted"))["items"]
    assert seg[0]["flags"] == ["no_speech"]
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], expect=409, json={"text": "edit after submit"})   # locked
    await env.call("POST", f"/segments/{sid}/annotation/submit", token=t["a1"], expect=409)


async def annotate_and_submit(env, tok, did, n=1, text="انا عندي meeting"):
    r = await env.call("POST", f"/datasets/{did}/claims", token=tok, json={"minutes": n * 0.1})
    ids = r["segment_ids"][:n]
    for i, sid in enumerate(ids):
        await env.call("PUT", f"/segments/{sid}/annotation", token=tok, json={"text": f"{text} {i}"})
        await env.call("POST", f"/segments/{sid}/annotation/submit", token=tok)
    return ids


async def test_review_state_machine_comments_and_supersede(env):
    s = await setup_dataset(env, n_utts=4)
    did, t = s["dataset"]["id"], await team(env)
    [sid] = await annotate_and_submit(env, t["a1"], did)
    rv = lambda body, tok=None: env.call("POST", f"/segments/{sid}/annotation/review", token=tok or t["rv"], json=body, expect=(200, 201, 202, 403, 409, 422))  # noqa: E731
    async def code(body, tok=None):
        return (await env.client.post(f"/api/v1/segments/{sid}/annotation/review", headers=env.auth(tok or t["rv"]), json=body)).status_code
    assert await code({"action": "approve"}, t["a2"]) == 403                      # annotators cannot review
    assert await code({"action": "request_changes"}) == 422                       # comment required
    q = await env.call("GET", "/review/queue", token=t["rv"])
    assert q["total"] == 1 and q["items"][0]["segment"]["id"] == sid
    assert (await env.call("GET", "/review/queue", token=t["a1"], expect=403)) is not None
    ch = await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "request_changes", "note": "last word missing"})
    assert ch["status"] == "changes_requested" and ch["claimed_by"] == t["u1"]["id"]      # back in the author's batch
    w = await env.call("GET", f"/segments/{sid}/annotation", token=t["a1"])
    assert w["comments"][0]["body"] == "last word missing" and not w["comments"][0]["resolved"]
    # author edits -> draft (new version), replies, resolves, resubmits
    v = await env.call("PUT", f"/segments/{sid}/annotation", token=t["a1"], json={"text": "انا عندي meeting اليوم"})
    assert v["status"] == "draft" and v["version"] == ch["version"] + 1
    cid = w["comments"][0]["id"]
    await env.call("POST", f"/annotations/{ch['id']}/comments", token=t["a1"], json={"body": "fixed", "parent_id": cid})
    await env.call("PATCH", f"/comments/{cid}", token=t["a1"], json={"resolved": True})
    await env.call("POST", f"/segments/{sid}/annotation/submit", token=t["a1"])
    assert await code({"action": "approve"}, t["a1"]) == 403                       # annotators still no
    # reviewer == author is forbidden even for admin-like reviewers
    [sid2] = await annotate_and_submit(env, t["rv"], did)
    assert (await env.client.post(f"/api/v1/segments/{sid2}/annotation/review", headers=env.auth(t["rv"]), json={"action": "approve"})).status_code == 403
    st = await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "start"})
    assert st["status"] == "in_review" and st["reviewer_id"] == t["ur"]["id"]
    ap = await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "approve"})
    assert ap["status"] == "approved"
    seg = (await env.call("GET", f"/datasets/{did}/segments?status=approved"))["items"][0]
    assert seg["strata"]["lang_mix"] == "mixed"
    # edit & approve a second round: reviewer becomes co-author, older approved superseded
    await env.call("POST", f"/segments/{sid}/annotation/reopen", token=t["rv"])
    await env.call("PUT", f"/segments/{sid}/annotation", token=t["rv"], json={"text": "نسخة معدلة v2"})
    await env.call("POST", f"/segments/{sid}/annotation/submit", token=t["rv"])
    adm = await env.call("POST", f"/segments/{sid}/annotation/review", json={"action": "approve", "text": "نسخة نهائية final"})  # admin token
    assert adm["status"] == "approved" and adm["text"] == "نسخة نهائية final" and adm["author_id"] == t["ur"]["id"]
    hist = (await env.call("GET", f"/segments/{sid}/annotation", token=t["a1"]))["history"]
    approved = [h for h in hist if h["status"] == "approved"]
    assert len(approved) == 1 and approved[0]["text"] == "نسخة نهائية final"          # only one approved at a time
    assert "superseded" in {h["status"] for h in hist}
    await env.call("PATCH", f"/comments/{cid}", token=t["a2"], json={"body": "hijack"}, expect=403)


async def test_batch_approve_requires_spot_check(env):
    s = await setup_dataset(env, n_utts=10)
    did, t = s["dataset"]["id"], await team(env)
    ids = await annotate_and_submit(env, t["a1"], did, n=5)
    plan = await env.call("POST", "/review/batch-approve/plan", token=t["rv"], json={"segment_ids": ids})
    assert 1 <= len(plan["spot_check_ids"]) <= 1 + len(ids) // 10 and set(plan["spot_check_ids"]) <= set(ids)
    bad = await env.call("POST", "/review/batch-approve", token=t["rv"], expect=422, json={"segment_ids": ids, "listened_ids": []})
    assert bad["error"]["spot_check_ids"] == plan["spot_check_ids"]
    ok = await env.call("POST", "/review/batch-approve", token=t["rv"], json={"segment_ids": ids, "listened_ids": plan["spot_check_ids"]})
    assert sorted(ok["approved"]) == sorted(ids) and not ok["failed"]
    stats = await env.call("GET", f"/review/author-stats/{t['u1']['id']}", token=t["rv"])
    assert stats["reviewed"] == 5 and stats["approval_rate"] == 1.0


async def test_review_queue_filters(env):
    s = await setup_dataset(env, n_utts=6)
    did, t = s["dataset"]["id"], await team(env)
    ids1 = await annotate_and_submit(env, t["a1"], did, n=2)
    ids2 = await annotate_and_submit(env, t["a2"], did, n=1)
    await env.call("PATCH", f"/segments/{ids1[0]}", json={"flags": ["overlap"], "strata": {"dialect": "gulf"}})
    await env.call("POST", f"/annotations/{(await env.call('GET', f'/segments/{ids1[1]}/annotation', token=t['rv']))['annotation']['id']}/comments", token=t["rv"], json={"body": "hm"})
    f = lambda qs: env.call("GET", f"/review/queue?{qs}", token=t["rv"])  # noqa: E731
    assert (await f(""))["total"] == 3
    assert (await f(f"annotator_id={t['u2']['id']}"))["total"] == 1
    assert (await f("dialect=gulf"))["total"] == 1 and (await f("flag=overlap"))["total"] == 1
    assert (await f("has_comments=true"))["total"] == 1 and (await f("has_comments=false"))["total"] == 2
    assert (await f(f"dataset={did}&limit=2"))["next_cursor"] == "2"


async def test_gold_freeze_versions_policy_and_immutability(env):
    s = await setup_dataset(env, n_utts=8)
    did, t = s["dataset"]["id"], await team(env)
    await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": "empty"}, expect=422)               # nothing approved
    ids = await annotate_and_submit(env, t["a1"], did, n=4)
    for sid in ids:
        await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "approve"})
    await env.call("PATCH", f"/segments/{ids[0]}", json={"flags": ["unintelligible"]})
    await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": "g", "policy": {"min_approvals": 2}}, expect=422)
    g1 = await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": "gold-q4"})
    assert g1["version"] == 1 and g1["segment_count"] == 3                      # unintelligible excluded by policy
    assert g1["policy"]["exclude_flags"] == ["unintelligible"] and g1["minutes"] > 0
    assert g1["strata_distribution"]["lang_mix"] == {"mixed": 3}
    more = await annotate_and_submit(env, t["a2"], did, n=2, text="ثلاثة")
    for sid in more:
        await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "approve"})
    g2 = await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": "gold-q4-b"})
    assert g2["version"] == 2 and g2["segment_count"] == 5
    again = await env.call("GET", f"/gold-sets/{g1['id']}")
    assert again["segment_count"] == 3                                          # v1 is immutable
    items = (await env.call("GET", f"/gold-sets/{g1['id']}/items"))["items"]
    # later edits to the live annotation never change a frozen item
    await env.call("POST", f"/segments/{ids[1]}/annotation/reopen", token=t["rv"])
    await env.call("PUT", f"/segments/{ids[1]}/annotation", token=t["rv"], json={"text": "CHANGED"})
    items2 = (await env.call("GET", f"/gold-sets/{g1['id']}/items"))["items"]
    assert items == items2 and all("CHANGED" not in i["text"] for i in items2)
    assert len((await env.call("GET", f"/datasets/{did}/gold-sets"))["items"]) == 2
    assert (await env.call("GET", f"/datasets/{did}"))["gold_sets"][0]["version"] == 1


async def test_iaa_double_annotation_and_agreement(env):
    s = await setup_dataset(env, n_utts=10)
    did, t = s["dataset"]["id"], await team(env)
    ids = await annotate_and_submit(env, t["a1"], did, n=5, text="the quick brown fox")
    for sid in ids:
        await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "approve"})
    smp = await env.call("POST", f"/datasets/{did}/iaa/sample", json={"pct": 40, "seed": 1})
    assert smp["marked"] == 2
    # the original author cannot be the second annotator; bob can, blind (no primary text exposed)
    none = await env.call("POST", f"/datasets/{did}/claims", token=t["a1"], json={"minutes": 5, "strategy": "iaa"})
    assert none["segment_ids"] == []
    got = await env.call("POST", f"/datasets/{did}/claims", token=t["a2"], json={"minutes": 5, "strategy": "iaa"})
    assert len(got["segment_ids"]) == 2 and got["track"] == "iaa"
    for i, sid in enumerate(got["segment_ids"]):
        blind = await env.call("GET", f"/segments/{sid}/annotation?track=iaa", token=t["a2"])
        assert blind["annotation"]["text"] == "" and blind["history"][0]["track"] == "iaa"
        txt = "the quick brown fox %d" % i if i == 0 else "a quick brown dog %d" % i
        await env.call("PUT", f"/segments/{sid}/annotation?track=iaa", token=t["a2"], json={"text": txt})
        await env.call("POST", f"/segments/{sid}/annotation/submit?track=iaa", token=t["a2"])
    res = await env.call("GET", f"/datasets/{did}/iaa")
    assert res["pairs"] == 2 and 0 < res["wer"] < 1 and res["per_annotator"][0]["user_id"] == t["u2"]["id"]
    assert res["agreement"] == pytest.approx(1 - res["wer"])
    # iaa rows never leak into review queue, segment status, or gold
    assert (await env.call("GET", "/review/queue", token=t["rv"]))["total"] == 0
    g = await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": "g"})
    assert g["segment_count"] == 5
    scoped = await env.call("GET", f"/datasets/{did}/iaa?gold_set=v1")
    assert scoped["pairs"] == 2


async def test_export_import_round_trip_is_byte_identical(env):
    s = await setup_dataset(env, n_utts=6)
    did, t = s["dataset"]["id"], await team(env)
    ids = await annotate_and_submit(env, t["a1"], did, n=3, text="مرحبا hello")
    for sid in ids:
        await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "approve"})
    await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": "gold-rt"})
    z1 = await env.call("GET", f"/datasets/{did}/export?gold_set=v1")
    assert z1.headers["content-type"] == "application/zip" and z1.headers["x-mizan-segments"] == "3"
    # import into a brand-new dataset
    d2 = await env.call("POST", f"/projects/{s['project']['id']}/datasets", json={"name": "copy"})
    j = await env.call("POST", f"/datasets/{d2['id']}/import", files={"file": ("g.zip", z1.content, "application/zip")})
    await env.drain()
    job = await env.call("GET", f"/jobs/{j['job_id']}")
    assert job["status"] == "succeeded" and job["result"]["segments_created"] == 3 and job["result"]["gold_version"] == 1
    z2 = await env.call("GET", f"/datasets/{d2['id']}/export?gold_set=v1")
    assert z2.content == z1.content                                              # byte-identical round trip
    gs = (await env.call("GET", f"/datasets/{d2['id']}/gold-sets"))["items"][0]
    assert gs["name"] == "gold-rt" and gs["segment_count"] == 3
    a = (await env.call("GET", f"/datasets/{d2['id']}/audio"))["items"][0]
    assert a["status"] == "ready" and a["duration_s"] == pytest.approx(s["segments"][0]["end_s"] * 0 + a["duration_s"])
    # garbage archives are rejected cleanly
    j2 = await env.call("POST", f"/datasets/{d2['id']}/import", files={"file": ("x.zip", b"nope", "application/zip")})
    await env.drain()
    assert (await env.call("GET", f"/jobs/{j2['job_id']}"))["status"] == "failed"


async def test_queue_segment_prioritises_and_reopens(env):
    s = await setup_dataset(env, n_utts=6)
    did, t = s["dataset"]["id"], await team(env)
    [sid] = await annotate_and_submit(env, t["a1"], did, n=1)
    await env.call("POST", f"/segments/{sid}/annotation/review", token=t["rv"], json={"action": "approve"})
    target = s["segments"][-1]["id"]
    await env.call("POST", f"/segments/{target}/queue", json={"priority": 5}, token=t["rv"])
    await env.call("POST", f"/segments/{sid}/queue", json={"priority": 9}, token=t["rv"])
    first = await env.call("POST", f"/datasets/{did}/claims", token=t["a2"], json={"minutes": 0.05})
    assert first["segment_ids"][0] == sid                                         # highest priority first, reopened as draft
    assert (await env.call("GET", f"/segments/{sid}/annotation", token=t["a2"]))["annotation"]["status"] == "draft"
    await env.call("POST", f"/segments/{sid}/queue", json={"priority": 1}, token=t["a2"], expect=403)


async def test_team_contributions_and_privacy(env):
    s = await setup_dataset(env, n_utts=8)
    did, t = s["dataset"]["id"], await team(env)
    ids = await annotate_and_submit(env, t["a1"], did, n=3)
    await env.call("POST", f"/segments/{ids[0]}/annotation/review", token=t["rv"], json={"action": "approve"})
    await env.call("POST", f"/segments/{ids[1]}/annotation/review", token=t["rv"], json={"action": "request_changes", "note": "no"})
    admin_view = {u["name"]: u for u in (await env.call("GET", "/team/contributions"))["users"]}
    ann = admin_view["Ann"]
    assert ann["segments_annotated"] == 3 and ann["minutes_annotated"] > 0 and ann["approval_rate"] == 0.5 and ann["streak_days"] == 1
    assert admin_view["Rae"]["segments_reviewed"] == 2
    bob_view = (await env.call("GET", "/team/contributions", token=t["a2"]))["users"]
    assert [u["name"] for u in bob_view] == ["Bob"] and (await env.call("GET", "/team/contributions", token=t["a2"]))["leaderboard"] == []
    await env.call("PATCH", "/me/preferences", token=t["a1"], json={"leaderboard_opt_in": True})
    bob2 = await env.call("GET", "/team/contributions", token=t["a2"])
    ann_pub = next(u for u in bob2["users"] if u["name"] == "Ann")
    assert ann_pub["approval_rate"] is None and ann_pub["iaa"] is None and ann_pub["minutes_annotated"] > 0     # no per-user quality publicly
    assert bob2["leaderboard"][0]["name"] == "Ann" and "approval_rate" not in bob2["leaderboard"][0]
    rev_view = {u["name"]: u for u in (await env.call("GET", "/team/contributions", token=t["rv"]))["users"]}
    assert rev_view["Ann"]["approval_rate"] == 0.5                                # reviewers see it (tooltip)
