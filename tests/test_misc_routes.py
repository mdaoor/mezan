async def test_catalogue_and_listing_routes(env):
    st = await env.call("GET", "/strategies")
    assert {s["kind"] for s in st["segmenters"]} >= {"silero_vad", "energy_vad", "fixed_window", "manual_import"}
    assert "arabic_v1" in {n["kind"] for n in st["normalizers"]} and st["metric_presets"]["arabic_codeswitch_default"][0] == "wer"
    assert next(n for n in st["normalizers"] if n["kind"] == "arabic_v1")["config_schema"]["properties"]["hamza_on_waw_yaa"]["default"] is True
    assert {m["id"] for m in st["metrics"]} >= {"wer", "rtfx"} and any(e["kind"] == "markdown" for e in st["exporters"])
    pr = await env.call("GET", "/model-endpoint-presets")
    assert {p["engine_kind"] for p in pr["items"]} >= {"vllm_openai", "faster_whisper_http"} and pr["items"][0]["model_id"] == "IFM/Jais-ASR"


async def test_project_endpoint_user_and_campaign_listing(env):
    p = await env.call("POST", "/projects", json={"name": "P1", "description": "d"})
    d = await env.call("POST", f"/projects/{p['id']}/datasets", json={"name": "D1"})
    got = await env.call("GET", f"/projects/{p['id']}")
    assert got["datasets"][0]["id"] == d["id"]
    ds = await env.call("GET", f"/projects/{p['id']}/datasets")
    assert ds["items"][0]["stats"]["segments"] == 0 and ds["items"][0]["name"] == "D1"
    ep = await env.call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": "m", "display_name": "mock"})
    assert [e["id"] for e in (await env.call("GET", "/model-endpoints"))["items"]] == [ep["id"]]
    assert (await env.call("GET", f"/model-endpoints/{ep['id']}/models"))["models"] == ["m"]
    bad = await env.call("POST", "/model-endpoints", json={"engine_kind": "vllm_openai", "model_id": "m", "display_name": "x", "base_url": "http://127.0.0.1:1"})
    await env.call("GET", f"/model-endpoints/{bad['id']}/models", expect=422)               # unreachable -> clean error, not 500
    tok, u = await env.make_user("Zed", "annotator")
    users = (await env.call("GET", "/users"))["items"]
    assert {x["name"] for x in users} >= {"Admin", "Zed"}
    up = await env.call("PATCH", f"/users/{u['id']}", json={"role": "reviewer", "name": "Zed Z"})
    assert up["role"] == "reviewer" and up["name"] == "Zed Z"
    await env.call("POST", "/projects", json={"name": "x"}, token=tok)                       # role change applies immediately (role read from DB)
    me = await env.call("GET", "/me", token=tok)
    admin_id = (await env.call("GET", "/me"))["id"]
    await env.call("PATCH", f"/users/{admin_id}", json={"role": "viewer"}, expect=409)       # cannot demote self
    await env.call("PATCH", f"/users/{u['id']}", json={"role": "bogus"}, expect=422)
    cp = await env.call("POST", "/campaigns", json={"project_id": p["id"], "name": "C"})
    assert [c["id"] for c in (await env.call("GET", f"/campaigns?project_id={p['id']}"))["items"]] == [cp["id"]]
    assert len((await env.call("GET", "/campaigns"))["items"]) == 1
