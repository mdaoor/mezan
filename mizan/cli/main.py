"""`mizan` CLI: thin wrappers over the REST API (spec 6.9). Works in CI: set MIZAN_URL and MIZAN_TOKEN."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import typer

app = typer.Typer(help="Mizan: collaborative voice-model evaluation workbench", no_args_is_help=True)
run_app = typer.Typer(help="Start evaluation or load runs", no_args_is_help=True)
app.add_typer(run_app, name="run")

CONFIG = Path(os.environ.get("MIZAN_CONFIG", Path.home() / ".config" / "mizan" / "config.json"))


def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


class API:
    def __init__(self):
        cfg = _cfg()
        self.base = (os.environ.get("MIZAN_URL") or cfg.get("url") or "http://localhost:8080").rstrip("/") + "/api/v1"
        tok = os.environ.get("MIZAN_TOKEN") or cfg.get("token")
        self.http = httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {tok}"} if tok else {}, timeout=300)

    def call(self, method: str, path: str, **kw):
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            typer.secho(f"cannot reach {self.base}: {e}", fg="red", err=True)
            raise typer.Exit(2)
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or r.json()
                msg = err.get("message") if isinstance(err, dict) else err
            except ValueError:
                msg = r.text[:300]
            typer.secho(f"{method} {path} -> {r.status_code}: {msg}", fg="red", err=True)
            raise typer.Exit(1)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    def get(self, path, **kw): return self.call("GET", path, **kw)
    def post(self, path, **kw): return self.call("POST", path, **kw)


def _out(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _wait(api: API, kind: str, id_: str, timeout: float) -> dict:
    """Poll a job / run / load-run / report until it reaches a terminal state."""
    path = {"job": f"/jobs/{id_}", "run": f"/runs/{id_}", "load": f"/load-runs/{id_}", "report": f"/reports/{id_}"}[kind]
    done = {"succeeded", "failed", "cancelled", "ready"}
    t0, last = time.time(), None
    while True:
        d = api.get(path)
        prog = d.get("progress")
        line = f"{kind} {id_[:8]} {d['status']}" + (f" {prog['done']}/{prog['total']}" if isinstance(prog, dict) and prog.get("total") else "")
        if line != last and sys.stderr.isatty() or d["status"] in done:
            typer.echo(line, err=True)
        last = line
        if d["status"] in done:
            return d
        if time.time() - t0 > timeout:
            typer.secho("timed out waiting", fg="red", err=True)
            raise typer.Exit(3)
        time.sleep(1.0)


def _resolve_endpoint(api: API, ref: str) -> str:
    items = api.get("/model-endpoints")["items"]
    for key in ("id", "display_name", "model_id"):
        hits = [e for e in items if e[key] == ref]
        if len(hits) == 1:
            return hits[0]["id"]
        if len(hits) > 1:
            typer.secho(f"'{ref}' is ambiguous; use the endpoint id", fg="red", err=True)
            raise typer.Exit(1)
    typer.secho(f"no model endpoint matches '{ref}'. known: {[e['display_name'] for e in items]}", fg="red", err=True)
    raise typer.Exit(1)


def _resolve_gold(api: API, ref: str, dataset: Optional[str]) -> tuple[str, str]:
    """('v1', dataset) or gold-set id -> (gold_set_id, dataset_id)."""
    if ref.lower().lstrip("v").isdigit():
        if not dataset:
            typer.secho("--dataset is required when --gold is a version like v1", fg="red", err=True)
            raise typer.Exit(1)
        for g in api.get(f"/datasets/{dataset}/gold-sets")["items"]:
            if g["version"] == int(ref.lower().lstrip("v")):
                return g["id"], dataset
        typer.secho(f"dataset has no gold set {ref}", fg="red", err=True)
        raise typer.Exit(1)
    g = api.get(f"/gold-sets/{ref}")
    return g["id"], g["dataset_id"]


# ------------------------------------------------------------------- session
@app.command()
def login(url: str = typer.Option("http://localhost:8080", envvar="MIZAN_URL"), email: str = typer.Option(..., prompt=True),
          password: str = typer.Option(..., prompt=True, hide_input=True)):
    """Authenticate and store the token in ~/.config/mizan/config.json."""
    r = httpx.post(url.rstrip("/") + "/api/v1/auth/login", json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        typer.secho("login failed", fg="red", err=True)
        raise typer.Exit(1)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"url": url, "token": r.json()["access_token"]}))
    CONFIG.chmod(0o600)
    typer.echo(f"logged in as {r.json()['user']['name']} ({r.json()['user']['role']})")


@app.command()
def whoami():
    _out(API().get("/me"))


# ------------------------------------------------------------------- objects
@app.command("project")
def project_create(name: str, description: str = ""):
    """Create a project."""
    _out(API().post("/projects", json={"name": name, "description": description}))


@app.command("dataset")
def dataset_create(project: str, name: str, languages: str = typer.Option("ar,en", help="comma-separated language hints")):
    """Create a dataset in a project."""
    _out(API().post(f"/projects/{project}/datasets", json={"name": name, "language_hints": languages.split(",")}))


@app.command("endpoint")
def endpoint_add(kind: str = typer.Option(..., "--kind"), model: str = typer.Option(..., "--model"), name: str = typer.Option(..., "--name"),
                 url: str = typer.Option("", "--url"), config: str = typer.Option("{}", help="JSON adapter config"),
                 test: bool = typer.Option(True, help="run the connection test after creating")):
    """Register a model endpoint (engine kind + model + URL)."""
    api = API()
    ep = api.post("/model-endpoints", json={"engine_kind": kind, "model_id": model, "display_name": name, "base_url": url, "config": json.loads(config)})
    _out(ep)
    if test:
        t = api.post(f"/model-endpoints/{ep['id']}/test")
        typer.secho(("OK " if t["ok"] else "FAILED ") + json.dumps(t["smoke"], ensure_ascii=False), fg="green" if t["ok"] else "red", err=True)


@app.command("engines")
def engines():
    """List engine kinds available on the server."""
    for e in API().get("/engines")["items"]:
        typer.echo(f"{e['kind']:22} v{e['version']:8} {'EXTERNAL ' if e['external'] else '         '}{e['name']}")


# --------------------------------------------------------------- pipeline
@app.command()
def upload(dataset: str, files: list[Path], segmenter: Optional[str] = typer.Option(None, help="auto-segment after ingest"),
           wait: bool = typer.Option(True, help="wait for ingest to finish")):
    """Upload audio files (wav/mp3/m4a/mp4/ogg/flac) to a dataset."""
    api = API()
    handles = [("files", (f.name, f.open("rb"), "application/octet-stream")) for f in files]
    r = api.post(f"/datasets/{dataset}/audio", files=handles, params={"segmenter": segmenter} if segmenter else None)
    _out(r)
    if wait and r.get("job_id"):
        j = _wait(api, "job", r["job_id"], 3600)
        if j["status"] != "succeeded" or j["result"].get("failed"):
            raise typer.Exit(1)


@app.command()
def segment(dataset: str, segmenter: str = "energy_vad", config: str = typer.Option("{}", help="JSON segmenter config"), wait: bool = True):
    """Auto-segment a dataset (VAD)."""
    api = API()
    r = api.post(f"/datasets/{dataset}/segment", json={"segmenter": {"kind": segmenter, "config": json.loads(config)}})
    if wait:
        _out(_wait(api, "job", r["job_id"], 3600)["result"])
    else:
        _out(r)


@app.command()
def bootstrap(dataset: str = typer.Option(..., "--dataset"), model: str = typer.Option(..., "--model"), concurrency: Optional[int] = None,
              language: Optional[str] = None, wait: bool = True):
    """Generate machine draft transcripts for annotators to correct."""
    api = API()
    opts = {k: v for k, v in (("concurrency", concurrency), ("language_hint", language)) if v}
    r = api.post(f"/datasets/{dataset}/bootstrap", json={"model_endpoint_id": _resolve_endpoint(api, model), "options": opts})
    _out(_wait(api, "run", r["run_id"], 7200) if wait else r)


@app.command("freeze-gold")
def freeze_gold(dataset: str, name: str, policy: str = typer.Option("{}", help="JSON policy")):
    """Freeze approved annotations into a new immutable gold set version."""
    _out(API().post(f"/datasets/{dataset}/gold-sets", json={"name": name, "policy": json.loads(policy)}))


@run_app.command("eval")
def run_eval(model: list[str] = typer.Option(..., "--model", "--endpoint", help="endpoint id/name; repeat for several"),
             gold: Optional[str] = typer.Option(None, help="gold set: version like v1 (needs --dataset) or id"),
             ref_run: Optional[str] = typer.Option(None, "--ref-run", help="agreement mode: score against another run"),
             dataset: Optional[str] = None, normalizer: str = "arabic_v1", metrics: str = "arabic_codeswitch_default",
             concurrency: Optional[int] = None, language: Optional[str] = None, name: Optional[str] = None, wait: bool = True,
             fail_above_wer: Optional[float] = typer.Option(None, help="CI gate: exit 1 if overall WER exceeds this")):
    """Evaluate model(s) against a gold set (or another run)."""
    api = API()
    if bool(gold) == bool(ref_run):
        typer.secho("pass exactly one of --gold / --ref-run", fg="red", err=True)
        raise typer.Exit(1)
    if gold:
        ref_id, dataset = _resolve_gold(api, gold, dataset)
        ref = {"type": "gold_set", "id": ref_id}
    else:
        ref = {"type": "run", "id": ref_run}
    opts = {k: v for k, v in (("concurrency", concurrency), ("language_hint", language)) if v}
    runs = []
    for m_ in model:
        body = {"type": "evaluation", "name": name, "model_endpoint_id": _resolve_endpoint(api, m_), "reference": ref, "dataset_id": dataset,
                "normalizer": {"kind": normalizer}, "metric_set": metrics.split(",") if "," in metrics else metrics, "options": opts}
        runs.append(api.post("/runs", json=body)["run_id"])
    results = [_wait(api, "run", r, 24 * 3600) if wait else {"id": r} for r in runs]
    if wait:
        _out([{"run": r["id"], "name": r["name"], "status": r["status"], **{k: round(v, 4) for k, v in r["summary"].items()
              if k in ("wer", "cer", "mer", "wer_ar", "wer_en", "en_preservation", "hallucination_rate", "latency_ms_p95")}} for r in results])
        if any(r["status"] != "succeeded" for r in results):
            raise typer.Exit(1)
        if fail_above_wer is not None and any(r["summary"].get("wer", 1) > fail_above_wer for r in results):
            typer.secho(f"WER above {fail_above_wer}", fg="red", err=True)
            raise typer.Exit(1)
    else:
        _out(runs)


@run_app.command("load")
def run_load(scenario: str = typer.Option(..., help="scenario id or name"), model: str = typer.Option(..., "--model", "--endpoint"),
             dataset: str = typer.Option(..., "--dataset"), telemetry: Optional[str] = typer.Option(None, help="nvidia_smi | dcgm_prometheus | none"),
             wait: bool = True):
    """Run a load scenario against an endpoint."""
    api = API()
    sc = next((s for s in api.get("/load-scenarios")["items"] if scenario in (s["id"], s["name"])), None)
    if sc is None:
        typer.secho(f"unknown scenario '{scenario}'", fg="red", err=True)
        raise typer.Exit(1)
    r = api.post("/load-runs", json={"model_endpoint_id": _resolve_endpoint(api, model), "scenario_id": sc["id"], "dataset_id": dataset,
                                      "telemetry_source": telemetry})
    if not wait:
        _out(r)
        return
    d = _wait(api, "load", r["load_run_id"], 24 * 3600)
    d = api.get(f"/load-runs/{r['load_run_id']}")
    for s in d["steps"]:
        typer.echo(f"c={s['concurrency']:<3} p50={s['p50']:.0f}ms p95={s['p95']:.0f}ms p99={s['p99']:.0f}ms rtfx={s['rtfx']:.1f} rps={s['req_per_s']:.1f} err={s['error_rate']:.1%}")
    typer.echo(f"saturation: {d['saturation'] or 'not reached'}")
    if d["status"] != "succeeded":
        raise typer.Exit(1)


@app.command()
def campaign(project: str, name: str, question: str = "", runs: list[str] = typer.Option([], "--run"), loads: list[str] = typer.Option([], "--load")):
    """Create a campaign grouping evaluation and load runs."""
    _out(API().post("/campaigns", json={"project_id": project, "name": name, "question": question, "run_ids": runs, "load_run_ids": loads}))


@app.command()
def report(campaign: str = typer.Option(..., "--campaign"), format: str = typer.Option("md", "--format"), output: Optional[Path] = typer.Option(None, "-o"),
           new: bool = typer.Option(False, help="always generate a new report version")):
    """Generate (or reuse) the campaign report and export it."""
    api = API()
    existing = [r for r in api.get(f"/campaigns/{campaign}/reports")["items"] if r["status"] == "ready"]
    if new or not existing:
        r = api.post(f"/campaigns/{campaign}/reports", json={"template": "decision_v1"})
        rep = _wait(api, "report", r["report_id"], 3600)
        if rep["status"] != "ready":
            typer.secho(f"report failed: {rep.get('error')}", fg="red", err=True)
            raise typer.Exit(1)
        rid = r["report_id"]
    else:
        rid = existing[0]["id"]
    resp = api.get(f"/reports/{rid}/export", params={"format": format})
    data = resp.content if hasattr(resp, "content") else json.dumps(resp, ensure_ascii=False).encode()
    if output:
        output.write_bytes(data)
        typer.echo(f"wrote {output}", err=True)
    else:
        sys.stdout.buffer.write(data)


@app.command("export")
def export_dataset(dataset: str, gold: Optional[str] = None, output: Path = typer.Option(Path("dataset.zip"), "-o")):
    """Export a dataset / gold set as JSONL + audio zip."""
    api = API()
    r = api.get(f"/datasets/{dataset}/export", params={"gold_set": gold} if gold else None)
    output.write_bytes(r.content)
    typer.echo(f"wrote {output} ({len(r.content)} bytes)", err=True)


@app.command("import")
def import_dataset(dataset: str, archive: Path, wait: bool = True):
    """Import a Mizan dataset zip."""
    api = API()
    r = api.post(f"/datasets/{dataset}/import", files={"file": (archive.name, archive.open("rb"), "application/zip")})
    _out(_wait(api, "job", r["job_id"], 3600) if wait else r)


# --------------------------------------------------------------- server ops
@app.command()
def serve(host: str = "0.0.0.0", port: int = 8080, workers: int = 1, reload: bool = False):
    """Run the API (+ embedded worker unless MIZAN_EMBEDDED_WORKER=false)."""
    import uvicorn
    uvicorn.run("mizan.api.app:app_factory", factory=True, host=host, port=port, workers=workers, reload=reload)


@app.command()
def worker(queues: str = typer.Option("ingest,infer,score,load,report", help="comma-separated queues"), slots: int = 4):
    """Run a standalone worker process (scale horizontally; set MIZAN_EMBEDDED_WORKER=false on the API)."""
    import asyncio
    import logging

    from mizan.core.container import build_container, init_storage
    from mizan.core.jobs import Worker

    logging.basicConfig(level=logging.INFO)

    async def main():
        c = build_container()
        await init_storage(c)
        await c.hub.start()
        w = Worker(c, slots=slots, queues=queues.split(","))
        typer.echo(f"worker started: queues={queues} slots={slots}")
        try:
            await w.run_forever()
        finally:
            await w.stop()

    asyncio.run(main())


@app.command()
def migrate():
    """Apply database migrations (alembic upgrade head)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    app()
