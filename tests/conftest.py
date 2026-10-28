from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from mizan.api.app import create_app
from mizan.config import Settings
from tests.helpers import HIT, route_templates


class Env:
    def __init__(self, app, client, c):
        self.app, self.client, self.c = app, client, c
        self.token = ""

    def auth(self, token: str | None = None) -> dict:
        return {"Authorization": f"Bearer {token or self.token}"}

    async def call(self, method: str, path: str, expect: int | tuple = (200, 201, 202, 204), token: str | None = None, **kw):
        r = await self.client.request(method, f"/api/v1{path}", headers=self.auth(token), **kw)
        self._record(method, path)
        ok = (expect,) if isinstance(expect, int) else expect
        assert r.status_code in ok, f"{method} {path} -> {r.status_code}: {r.text[:500]}"
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else r

    def _record(self, method: str, path: str) -> None:
        url = "/api/v1" + path.split("?")[0]
        for tmpl, rx in route_templates(self.app):
            if rx.fullmatch(url) and method.lower() in self.app.openapi()["paths"][tmpl]:
                HIT.add((method, tmpl))
                return

    async def login(self, email: str, password: str) -> str:
        r = await self.client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    async def make_user(self, name: str, role: str) -> tuple[str, dict]:
        email = f"{name.lower()}@example.com"
        u = await self.call("POST", "/users", json={"email": email, "name": name, "role": role, "password": "secret123"})
        return await self.login(email, "secret123"), u

    async def drain(self, timeout: float = 60):
        await self.c.worker.drain(timeout)


@pytest_asyncio.fixture
async def env(tmp_path):
    s = Settings(data_dir=tmp_path / "data", database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", embedded_worker=True,
                 worker_slots=4, admin_email="admin@mizan.local", admin_password="admin", secret="test-secret-test-secret-test-secret")
    app = create_app(s)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as client:
            e = Env(app, client, app.state.c)
            e.token = await e.login("admin@mizan.local", "admin")
            yield e


import io
import wave

import pytest

from tests.helpers import utterances


async def setup_dataset(env: Env, n_utts: int = 12, ext: str = "wav", segmenter: str = "energy_vad", seed: int = 0) -> dict:
    """Project + dataset + uploaded & segmented synthetic audio."""
    p = await env.call("POST", "/projects", json={"name": "P"})
    d = await env.call("POST", f"/projects/{p['id']}/datasets", json={"name": "D", "language_hints": ["ar", "en"]})
    wav = utterances(n_utts, seed=seed)
    name = f"call-{seed}.{ext}"
    content = wav
    if ext != "wav":
        import asyncio, tempfile, pathlib
        src = pathlib.Path(tempfile.mkdtemp()) / "in.wav"
        src.write_bytes(wav)
        dst = src.with_suffix("." + ext)
        pr = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-y", "-i", str(src), str(dst), stderr=asyncio.subprocess.DEVNULL)
        await pr.wait()
        content = dst.read_bytes()
    up = await env.call("POST", f"/datasets/{d['id']}/audio", files=[("files", (name, content, "application/octet-stream"))])
    await env.drain()
    seg = await env.call("POST", f"/datasets/{d['id']}/segment", json={"segmenter": {"kind": segmenter}})
    await env.drain()
    segs = (await env.call("GET", f"/datasets/{d['id']}/segments?limit=1000"))["items"]
    return {"project": p, "dataset": d, "segments": segs, "upload": up, "audio_id": up["files"][0]["audio_file_id"]}


async def gold_from_texts(env: Env, s: dict, texts: list[str], flags: dict[int, list[str]] | None = None, name: str = "gold") -> dict:
    """Annotate/review/approve the first len(texts) segments via the real workflow and freeze a gold set."""
    did = s["dataset"]["id"]
    at, _ = await env.make_user(f"Ann{len(env.__dict__)}{id(texts) % 997}", "annotator")
    rt, _ = await env.make_user(f"Rev{len(env.__dict__)}{id(texts) % 991}", "reviewer")
    for i, (seg, text) in enumerate(zip(s["segments"], texts)):
        sid = seg["id"]
        if flags and i in flags:
            await env.call("PATCH", f"/segments/{sid}", json={"flags": flags[i]})
    r = await env.call("POST", f"/datasets/{did}/claims", token=at, json={"minutes": 60})
    claimed = set(r["segment_ids"])
    for i, (seg, text) in enumerate(zip(s["segments"], texts)):
        sid = seg["id"]
        assert sid in claimed
        await env.call("PUT", f"/segments/{sid}/annotation", token=at, json={"text": text, "flags": (flags or {}).get(i)})
        await env.call("POST", f"/segments/{sid}/annotation/submit", token=at)
        await env.call("POST", f"/segments/{sid}/annotation/review", token=rt, json={"action": "approve"})
    for sid in claimed - {x["id"] for x in s["segments"][: len(texts)]}:
        await env.call("DELETE", f"/claims/{sid}", token=at)
    return await env.call("POST", f"/datasets/{did}/gold-sets", json={"name": name, "policy": {"exclude_flags": []}})


async def scripted_endpoint(env: Env, name: str, s: dict, texts: list[str], kind: str = "counting", **cfg) -> dict:
    tr = {seg["id"]: t for seg, t in zip(s["segments"], texts)}
    return await env.call("POST", "/model-endpoints", json={"engine_kind": kind, "model_id": name, "display_name": name, "base_url": "x://",
                                                            "config": {"transcripts": tr, "latency_ms": 2, **cfg}})
