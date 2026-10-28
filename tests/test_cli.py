"""CLI against a live uvicorn server (thread) with the demo seed script."""
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from typer.testing import CliRunner

from mizan.api.app import create_app
from mizan.cli.main import app as cli
from mizan.config import Settings
from tests.helpers import utterances


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cli")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    app = create_app(Settings(data_dir=tmp / "d", database_url=f"sqlite+aiosqlite:///{tmp}/c.db", secret="x" * 40))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", tmp
    srv.should_exit = True
    t.join(10)


def test_cli_pipeline_and_ci_gate(server, tmp_path, monkeypatch):
    url, _ = server
    cfg = tmp_path / "cfg.json"
    monkeypatch.setenv("MIZAN_CONFIG", str(cfg))
    monkeypatch.setattr("mizan.cli.main.CONFIG", cfg)
    monkeypatch.setenv("MIZAN_URL", url)
    r = CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()
    run = lambda *a: r.invoke(cli, list(a), catch_exceptions=False)  # noqa: E731
    res = run("login", "--url", url, "--email", "admin@mizan.local", "--password", "admin")
    assert res.exit_code == 0 and "logged in" in res.stdout
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert "vllm_openai" in run("engines").stdout
    import json
    pid = json.loads(run("project", "P").stdout)["id"]
    did = json.loads(run("dataset", pid, "D").stdout)["id"]
    wav = tmp_path / "a.wav"
    wav.write_bytes(utterances(5))
    assert run("upload", did, str(wav), "--segmenter", "energy_vad").exit_code == 0
    time.sleep(1.5)
    segs = json.loads(subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {json.loads(cfg.read_text())['token']}", f"{url}/api/v1/datasets/{did}/segments"],
                                     capture_output=True, text=True).stdout)["items"]
    assert len(segs) == 5
    ep = json.loads(run("endpoint", "--kind", "mock", "--model", "m", "--name", "cli-mock", "--config", '{"default_text":"hello","latency_ms":2}').stdout)
    assert run("bootstrap", "--dataset", did, "--model", "cli-mock").exit_code == 0
    # unknown endpoint is a clear error, not a traceback
    bad = r.invoke(cli, ["bootstrap", "--dataset", did, "--model", "nope"])
    assert bad.exit_code == 1
    assert r.invoke(cli, ["run", "eval", "--model", "cli-mock"]).exit_code == 1            # needs --gold / --ref-run


def test_seed_demo_then_cli_eval_load_report(server, tmp_path, monkeypatch):
    url, _ = server
    cfg = tmp_path / "cfg2.json"
    monkeypatch.setattr("mizan.cli.main.CONFIG", cfg)
    monkeypatch.setenv("MIZAN_URL", url)
    out = subprocess.run([sys.executable, str(__import__("pathlib").Path(__file__).parents[1] / "contrib/seed_demo.py"), "--url", url], cwd=tmp_path, capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(__import__("pathlib").Path(__file__).parents[1])}, timeout=180)
    assert out.returncode == 0, out.stderr[-500:] + out.stdout[-500:]
    md = (tmp_path / "demo-report.md").read_text(encoding="utf-8")
    assert "Recommended endpoint: Demo Model A" in md and "## 8. Decision matrix" in md
    r = CliRunner()
    import json
    assert r.invoke(cli, ["login", "--url", url, "--email", "admin@mizan.local", "--password", "admin"]).exit_code == 0
    projects = json.loads(r.invoke(cli, ["whoami"]).stdout)
    assert projects["role"] == "admin"
    import httpx
    h = {"Authorization": "Bearer " + json.loads(cfg.read_text())["token"]}
    ds = httpx.get(f"{url}/api/v1/projects", headers=h).json()["items"][0]
    did = httpx.get(f"{url}/api/v1/projects/{ds['id']}", headers=h).json()["datasets"][0]["id"]
    ok = r.invoke(cli, ["run", "eval", "--model", "Demo Model A", "--gold", "v1", "--dataset", did])
    assert ok.exit_code == 0, ok.stderr if hasattr(ok, "stderr") else ok.output
    rows = json.loads(ok.stdout)
    assert rows[0]["wer"] == 0.0 and rows[0]["status"] == "succeeded"
    gate = r.invoke(cli, ["run", "eval", "--model", "Demo Model B", "--gold", "v1", "--dataset", did, "--fail-above-wer", "0.01"])
    assert gate.exit_code == 1                                               # CI gate trips on WER
    assert r.invoke(cli, ["run", "eval", "--model", "Demo Model A", "--dataset", did]).exit_code == 1
    camp = httpx.get(f"{url}/api/v1/campaigns", headers=h).json()["items"][0]
    f = tmp_path / "r.md"
    assert r.invoke(cli, ["report", "--campaign", camp["id"], "--format", "md", "-o", str(f)]).exit_code == 0
    assert f.read_text(encoding="utf-8").startswith("# Demo: which model?")
    z = tmp_path / "g.zip"
    assert r.invoke(cli, ["export", did, "--gold", "v1", "-o", str(z)]).exit_code == 0 and z.stat().st_size > 1000
    ld = r.invoke(cli, ["run", "load", "--scenario", "ramp-1-64", "--model", "nope", "--dataset", did])
    assert ld.exit_code == 1
