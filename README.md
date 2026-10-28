# ميزان Mizan

Self-hosted workbench for **creating golden transcription data as a team**, running **any speech-to-text model through any
inference engine**, and producing **one report** that covers accuracy *and* load/latency.
Everything that varies (engine, model, segmenter, normalizer, metric, exporter, telemetry source) is a plugin behind one UI.

## Three commands

```bash
docker compose up -d --build                       # api + worker + postgres + redis  ->  http://localhost:8080
mizan upload <dataset-id> call.mp4 --segmenter energy_vad
mizan run eval --model "Jais-ASR (vLLM GPU0)" --gold v1 --dataset <dataset-id>
```

Sign in as `admin@mizan.local` with `$MIZAN_ADMIN_PASSWORD` (default `admin` — change it, and set `MIZAN_SECRET`).
Mizan itself needs no GPU. Inference servers (vLLM, faster-whisper, hosted APIs) are registered **by URL**; see
`contrib/vllm-compose.yml` for two vLLM servers pinned to GPU 0/1 and `contrib/faster_whisper_server.py`.

## Try it without GPUs (demo campaign in minutes)

```bash
pip install -e ".[dev]"            # Python 3.12+, ffmpeg on PATH
cd web && npm ci && npm run build && cd ..
MIZAN_DATA_DIR=./data mizan serve --port 8080 &
python contrib/seed_demo.py --url http://localhost:8080      # synthetic audio + mock engines -> full campaign + demo-report.md
```

## The workflow

1. **Datasets** — upload (wav/mp3/m4a/mp4/ogg/flac → 16 kHz mono WAV), auto-segment (VAD), *bootstrap* machine drafts with any endpoint.
2. **Annotate** — claim ~10 minutes of audio, fix the draft in the keyboard-driven workbench (`Ctrl+/` lists the shortcuts), submit.
3. **Review** — a different person approves or requests changes (threaded comments). Approved segments become gold.
4. **Freeze gold** — immutable, versioned snapshot (`v1`, `v2`…); export/import as JSONL + audio zip (round-trips byte-identically).
5. **Evaluate** — endpoint × gold set (or *agreement mode* against another run) with the Arabic-aware `arabic_v1` normalizer;
   WER/CER/MER, script-split WER, English-term preservation, hallucination & insertion rates, per stratum; compare runs; disagreement explorer.
6. **Load test** — concurrency ramp through the same adapter: p50/p95/p99, RTFx, error classes, saturation point, GPU telemetry.
7. **Campaign report** (`decision_v1`) — functional + non-functional + decision matrix with editable weights; export MD / HTML / JSON / CSV.

## Architecture in one paragraph

FastAPI + SQLAlchemy 2 (async) + a database-backed job queue; React SPA served by the API. Strategy plugins (`mizan/plugins`) at
six seams — `EngineAdapter`, `Segmenter`, `Normalizer`, `Metric`, `Exporter`, `TelemetrySource` — each with a JSON Schema that
drives the UI forms, so **adding an engine is one class + one line in `plugins.yaml`**. Runs are idempotent per segment
(`UNIQUE(run, segment)`), store their fully resolved config, and resume after a crash. See `DECISIONS.md`, `CHANGELOG.md`, `PHASE_*_NOTES.md`.

### Add an engine

```python
# my_engine.py  (put on PYTHONPATH)
from mizan.plugins.base import EngineAdapter, Capabilities, TranscriptResult
class MyEngine(EngineAdapter):
    kind = "my_engine"; version = "1.0.0"
    @classmethod
    def config_schema(cls): return {"type": "object", "required": ["base_url", "model"], "properties": {...}}
    def __init__(self, cfg): self.cfg = cfg
    def capabilities(self): return Capabilities(False, False, True, False, 30, 8, False)
    async def health(self): ...
    async def transcribe(self, audio, options) -> TranscriptResult: ...
```
```yaml
# plugins.yaml
adapters:
  - module: my_engine:MyEngine
```
Restart api/worker — the Engines page lists it with a generated config form. Raise `AdapterError(retryable, error_class)` for failures.

## Configuration (environment)

| Variable | Default | |
|---|---|---|
| `MIZAN_DATABASE_URL` | `sqlite+aiosqlite:///data/mizan.db` | use `postgresql+asyncpg://…` in production |
| `MIZAN_DATA_DIR` | `./data` | blobs (audio, peaks, reports) |
| `MIZAN_SECRET` | dev value | JWT signing key — **set it** |
| `MIZAN_ADMIN_EMAIL` / `MIZAN_ADMIN_PASSWORD` | `admin@mizan.local` / `admin` | seeded when no users exist |
| `MIZAN_EMBEDDED_WORKER` | `true` | run jobs inside the API process; `false` + `mizan worker` to scale out |
| `MIZAN_REDIS_URL` | – | optional: forwards worker events to the API's WebSockets |
| `MIZAN_CLAIM_TTL_MIN` | `60` | annotation claim lifetime |
| `MIZAN_AUTO_CREATE` | `true` | create tables on start (dev); production uses `mizan migrate` |

## Development

```bash
pytest                       # ~270 s: plugins, API, workers, E2E demo flow, CLI, WebSocket (+ Playwright keyboard test if web/dist is built)
cd web && npm run dev        # Vite on :5173, proxies /api to :8080
python scripts/export_openapi.py
```

## Backup & restore

`contrib/backup.sh` dumps Postgres and tars the blob volume. Restore: `gunzip -c db.sql.gz | docker compose exec -T postgres psql -U mizan mizan`
and untar `blobs.tgz` into the `mizan-data` volume.

## Privacy

Audio and transcripts never leave the host unless an endpoint uses an adapter with `external=true` (`fanar_api`, `openai_api`,
`aws_transcribe`). Those are badged in the registry and in the add-endpoint drawer.
