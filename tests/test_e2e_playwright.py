"""Playwright keyboard-flow test against a live server serving the built SPA (skips if web/dist or Playwright is missing)."""
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from mizan.api.app import create_app
from mizan.config import Settings

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not (ROOT / "web/dist/index.html").exists() or not shutil.which("node"), reason="web/dist not built or node missing")


def test_workbench_keyboard_flow(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    app = create_app(Settings(data_dir=tmp_path / "d", database_url=f"sqlite+aiosqlite:///{tmp_path}/e.db", secret="y" * 40, web_dir=str(ROOT / "web/dist")))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        env = {**os.environ, "MIZAN_URL": f"http://127.0.0.1:{port}", "NODE_PATH": subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()}
        r = subprocess.run(["node", str(ROOT / "web/e2e/keyboard.mjs")], env=env, capture_output=True, text=True, timeout=180)
        if "Cannot find module 'playwright'" in r.stderr:
            pytest.skip("playwright not installed")
        assert r.returncode == 0 and "OK keyboard flow" in r.stdout, r.stdout[-800:] + r.stderr[-1500:]
    finally:
        srv.should_exit = True
        t.join(10)
