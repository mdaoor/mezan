# Decisions

Items the spec marks **[OPEN]**, plus deviations forced by the build environment. Fixed spec decisions were followed.

| Topic | Choice | Why |
|---|---|---|
| Task queue **[OPEN]** | **Database-backed `jobs` table** + worker loop (`mizan/core/jobs.py`), not Arq/Redis streams | Runs are already idempotent per `(run, segment)`, so a queue only needs "this job should be executing". A row with a heartbeat gives crash recovery (`STALE_S=30`) and graceful shutdown re-queueing with no extra infrastructure. The same loop runs embedded in the API (default) or as `mizan worker --queues ingest,infer,score,load,report`. Redis stays in Compose and is used only to bridge worker→API WebSocket events (`MIZAN_REDIS_URL`); that bridge is implemented but was **not exercised in this environment**. |
| Object store **[OPEN]** | Local filesystem behind `BlobStore` (MinIO optional in the spec) | Allowed by §3.2. An S3 implementation only needs `local_path/put/read`; not shipped. |
| Charts **[OPEN]** | Recharts (UI), inline SVG (HTML report exporter) | Smallest dependency surface. |
| Auth | Local users, JWT (HS256). Role is re-read from the DB on every request | Demotions apply immediately. OIDC remains future work. |
| Default DB for dev/tests | SQLite (WAL, `BEGIN IMMEDIATE`) | No external service for `pytest`. Array columns are JSON lists so one schema fits SQLite and Postgres; JSON is JSONB on Postgres. |
| Default segmenter | `energy_vad` (numpy) ships and is the UI default; `silero_vad` is registered but needs `pip install mizan[silero]` | torch is heavy. **`silero_vad` is untested here** (not installed in the sandbox). |
| Scoring | Inline per segment in the inference worker (`workers/score.py`), aggregation at run end | Avoids a second queue hop; scoring is deterministic CPU work. |
| Telemetry | Collected inside the load job (1 Hz) rather than a separate singleton service | One fewer moving part; the telemetry source plugin interface is as specified. |
| WER alignment | `jiwer` for every error-rate metric; script attribution reuses the same alignment | `wer_ar + wer_en` errors always add up to `wer`. |
| `wer_ar`/`wer_en` insertions | Attributed by the *hypothesis* token's script (no reference token exists) | Spec attributes by reference script; insertions need a rule. |
| Empty reference | `N=0`: all hypothesis tokens count as insertions; they enter corpus-level WER | Hallucinated text on `no_speech` segments must be penalised. |
| Span-based metrics | Script detection on normalized tokens; stored spans drive rendering and `lang_mix` | Offsets do not survive normalization. |
| Decision matrix | accuracy = 1−WER; code-switch = mean(1−WER-AR, 1−WER-EN, EN-preservation); throughput = best error-free RTFx ÷ best endpoint; robustness = mean(1−halluc, 1−insertion, 1−failed share). Missing components are dropped and weights renormalised | Spec gives weights, not formulas. |
| Gold `min_approvals` | Only `1` supported (400-class error otherwise) | The workflow records one reviewer approval. |
| `audio_files.sha256` | unique **per dataset**, not globally | The same file may legitimately live in two datasets. |
