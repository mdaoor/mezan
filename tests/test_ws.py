"""WebSocket hub: run progress, dataset presence/claim events. Uses Starlette's sync TestClient (own event loop)."""
import io
import time

import pytest
from fastapi.testclient import TestClient

from mizan.api.app import create_app
from mizan.config import Settings
from tests.helpers import utterances


@pytest.fixture
def client(tmp_path):
    s = Settings(data_dir=tmp_path / "d", database_url=f"sqlite+aiosqlite:///{tmp_path}/ws.db", embedded_worker=True,
                 secret="test-secret-test-secret-test-secret", worker_slots=2)
    with TestClient(create_app(s)) as c:
        yield c


def login(client, email="admin@mizan.local", pw="admin"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["access_token"]


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def wait_job(client, tok, job_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/v1/jobs/{job_id}", headers=H(tok)).json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.1)
    raise TimeoutError


def test_ws_rejects_bad_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/ws?token=bad") as ws:
            ws.receive_json()


def test_ws_progress_presence_and_claim_events(client):
    tok = login(client)
    p = client.post("/api/v1/projects", headers=H(tok), json={"name": "P"}).json()
    d = client.post(f"/api/v1/projects/{p['id']}/datasets", headers=H(tok), json={"name": "D"}).json()
    ann = client.post("/api/v1/users", headers=H(tok), json={"email": "a@x.com", "name": "Ann", "role": "annotator", "password": "secret123"}).json()
    atok = login(client, "a@x.com", "secret123")
    up = client.post(f"/api/v1/datasets/{d['id']}/audio?segmenter=energy_vad", headers=H(tok), files=[("files", ("a.wav", utterances(4), "audio/wav"))]).json()
    wait_job(client, tok, up["job_id"])
    time.sleep(1.0)                                                           # chained segment job
    segs = client.get(f"/api/v1/datasets/{d['id']}/segments", headers=H(tok)).json()["items"]
    assert len(segs) == 4                                                     # upload ?segmenter= chains segmentation

    ep = client.post("/api/v1/model-endpoints", headers=H(tok), json={"engine_kind": "mock", "model_id": "m", "display_name": "m", "config": {"latency_ms": 40}}).json()
    with client.websocket_connect(f"/api/v1/ws?token={tok}") as ws, client.websocket_connect(f"/ws?token={atok}") as ws2:
        ws.send_json({"subscribe": f"dataset:{d['id']}"})
        snap = ws.receive_json()
        assert snap["type"] == "presence" and snap["users"] == []
        ws2.send_json({"presence": {"dataset": d["id"], "segment": segs[0]["id"]}})
        pres = ws.receive_json()
        assert pres["type"] == "presence" and pres["users"][0]["name"] == "Ann" and pres["users"][0]["segment_id"] == segs[0]["id"]
        client.post(f"/api/v1/datasets/{d['id']}/claims", headers=H(atok), json={"minutes": 0.1})
        ev = ws.receive_json()
        assert ev["type"] == "claimed" and ev["name"] == "Ann" and ev["topic"] == f"dataset:{d['id']}"
        # run progress + finished
        run = client.post("/api/v1/runs", headers=H(tok), json={"type": "bootstrap", "model_endpoint_id": ep["id"], "dataset_id": d["id"],
                                                                   "options": {"concurrency": 1, "overwrite_drafts": True}}).json()
        ws.send_json({"subscribe": f"run:{run['run_id']}"})
        seen = []
        for _ in range(40):
            e = ws.receive_json()
            if e.get("topic") == f"run:{run['run_id']}":
                seen.append(e["type"])
                if e["type"] == "finished":
                    assert e["status"] in ("succeeded", "failed")
                    break
        assert "finished" in seen
        ws2.close()
        gone = ws.receive_json()
        assert gone["type"] == "presence" and gone["users"] == []              # presence cleared on disconnect
