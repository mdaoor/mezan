import io
import wave

import pytest

from tests.conftest import setup_dataset
from tests.helpers import utterances


async def test_auth_required_and_login(env):
    r = await env.client.get("/api/v1/projects")
    assert r.status_code == 401
    r = await env.client.post("/api/v1/auth/login", json={"email": "admin@mizan.local", "password": "bad"})
    assert r.status_code == 401
    me = await env.call("GET", "/me")
    assert me["role"] == "admin" and me["email"] == "admin@mizan.local"


async def test_upload_transcode_segment_stream(env):
    s = await setup_dataset(env, n_utts=6, ext="mp3")             # mp3 -> 16 kHz mono wav
    audio = (await env.call("GET", f"/datasets/{s['dataset']['id']}/audio"))["items"][0]
    assert audio["status"] == "ready" and audio["sample_rate"] == 16000 and audio["channels"] == 1
    assert 30 < audio["duration_s"] < 40 and audio["segments"] == 6
    assert len(s["segments"]) == 6
    assert all(x["strata"]["duration_bucket"] in ("<5s", "5-15s") for x in s["segments"])
    # stream a segment: valid wav of the right length
    seg = s["segments"][2]
    r = await env.call("GET", f"/audio/{s['audio_id']}/stream?start={seg['start_s']}&end={seg['end_s']}")
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert w.getnframes() / 16000 == pytest.approx(seg["end_s"] - seg["start_s"], abs=0.01)
    full = await env.call("GET", f"/audio/{s['audio_id']}/stream")
    assert full.headers["content-type"] == "audio/wav" and len(full.content) > 1_000_000
    peaks = await env.call("GET", f"/audio/{s['audio_id']}/peaks")
    assert peaks["peaks_per_s"] == 20 and len(peaks["peaks"]) == pytest.approx(audio["duration_s"] * 20, abs=2)
    await env.call("GET", f"/audio/{s['audio_id']}/preview")
    await env.call("GET", f"/audio/{s['audio_id']}/stream?start=5&end=5", expect=422)


async def test_duplicate_upload_is_deduplicated(env):
    s = await setup_dataset(env, n_utts=3)
    again = await env.call("POST", f"/datasets/{s['dataset']['id']}/audio",
                           files=[("files", ("copy.wav", utterances(3), "audio/wav"))])
    assert again["files"][0]["duplicate"] and again["files"][0]["audio_file_id"] == s["audio_id"] and again["job_id"] is None


async def test_unsupported_and_corrupt_files(env):
    p = await env.call("POST", "/projects", json={"name": "P"})
    d = await env.call("POST", f"/projects/{p['id']}/datasets", json={"name": "D"})
    await env.call("POST", f"/datasets/{d['id']}/audio", expect=422, files=[("files", ("x.exe", b"MZ", "application/octet-stream"))])
    up = await env.call("POST", f"/datasets/{d['id']}/audio", files=[("files", ("bad.wav", b"not audio at all", "audio/wav"))])
    await env.drain()
    a = (await env.call("GET", f"/datasets/{d['id']}/audio"))["items"][0]
    assert a["status"] == "failed" and a["error"]
    job = await env.call("GET", f"/jobs/{up['job_id']}")
    assert job["status"] == "succeeded" and job["result"]["failed"]


async def test_resegment_blocked_when_work_exists_and_allowed_otherwise(env):
    s = await setup_dataset(env, n_utts=4)
    did = s["dataset"]["id"]
    job = await env.call("POST", f"/datasets/{did}/segment", json={"segmenter": {"kind": "fixed_window", "config": {"window_s": 10}}})
    await env.drain()
    segs = (await env.call("GET", f"/datasets/{did}/segments"))["items"]
    assert all(x["end_s"] - x["start_s"] <= 10.001 for x in segs) and len(segs) != 4
    # human work pins the segments
    sid = segs[0]["id"]
    await env.call("POST", f"/datasets/{did}/claims", json={"minutes": 1})
    await env.call("PUT", f"/segments/{sid}/annotation", json={"text": "x"})
    j = await env.call("POST", f"/datasets/{did}/segment", json={"segmenter": {"kind": "energy_vad"}})
    await env.drain()
    res = (await env.call("GET", f"/jobs/{j['job_id']}"))["result"]
    assert res["skipped"] and "human annotations" in res["skipped"][0]["reason"]


async def test_unknown_segmenter_and_flags_validation(env):
    s = await setup_dataset(env, n_utts=2)
    await env.call("POST", f"/datasets/{s['dataset']['id']}/segment", json={"segmenter": {"kind": "nope"}}, expect=422)
    sid = s["segments"][0]["id"]
    r = await env.call("PATCH", f"/segments/{sid}", json={"flags": ["overlap"], "strata": {"dialect": "gulf", "duration_bucket": "hack"}})
    assert r["flags"] == ["overlap"] and r["strata"]["dialect"] == "gulf" and r["strata"]["duration_bucket"] != "hack"
    await env.call("PATCH", f"/segments/{sid}", json={"flags": ["bogus"]}, expect=422)


async def test_rbac_viewer_cannot_write(env):
    vt, _ = await env.make_user("Vee", "viewer")
    await env.call("POST", "/projects", json={"name": "x"}, token=vt, expect=403)
    await env.call("GET", "/projects", token=vt)
    at, _ = await env.make_user("Ann", "annotator")
    await env.call("POST", "/projects", json={"name": "x"}, token=at, expect=403)   # managing projects needs reviewer+
    await env.call("POST", "/users", json={"email": "z@z.com", "name": "z", "role": "admin", "password": "secret123"}, token=at, expect=403)
