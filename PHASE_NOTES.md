# Phase notes (deviations and open items per phase)

**Phase 0 — Foundation.** Upload → ffmpeg 16 kHz mono PCM16 → `peaks.json` (20 peaks/s) → `preview.opus` → segmentation → range/cut streaming.
Segment audio is cut by byte offset from `normalized.wav`. Re-segmenting is refused (per file, reported in the job result) once segments have gold items,
evaluation results or human annotations. Not verified: the "1 h mp4 → player streams any segment in 200 ms" acceptance on real hardware — only synthetic ≤1 min files were run; the cut path is O(segment) (seek + read) so it should hold.

**Phase 1 — Inference core.** Adapters: `vllm_openai`, `http_openai_compat`, `faster_whisper_http`, `fanar_api`, `openai_api`, `aws_transcribe` (+ `mock`).
`fanar_api` uses Fanar's OpenAI-style route and a default model id that must be verified against a real account; `aws_transcribe` is tested only with fake boto clients.
The ≥48-in-flight/server and "bootstrap 1 h in <2 min on both GPUs" criteria need real GPUs and were not measured; concurrency, retry (3 attempts, jittered backoff),
cancel/pause/resume, crash-resume and fail-fast on a dead endpoint are covered by tests.

**Phase 2 — Annotation.** Claims are optimistic (conditional `UPDATE`) and re-query until the time budget is met; 4 parallel claimers never overlap (tested).
Editor deviations: mixed-script typing relies on `unicode-bidi: plaintext` in the textarea — the visual bidi isolates (U+2068/2069) are **not** inserted,
because they would desynchronise the highlighting mirror layer. Stored text is never altered. Claims don't slide on autosave (fixed TTL + `/claims/{id}/extend`).

**Phase 3 — Review & gold.** State machine, threaded comments, edit & approve (reviewer = co-author), batch approve with a deterministic 10 % spot-check, immutable versioned gold,
IAA via blind second annotations (`track=iaa`), byte-identical export/import. Comments attach to the annotation *version* they were made on; the UI shows the whole segment thread.

**Phase 4 — Scoring.** Metrics cross-checked against an independent jiwer oracle; Appendix C fixtures verbatim. Agreement mode is labelled everywhere and excluded from the accuracy component.

**Phase 5 — Load.** Guardrails: per-request timeout, step abort at >20 % errors (min 10 requests), endpoint `max_concurrency` cap, saturation = first step with p95 > 2× previous or errors > 1 %.
`nvidia_smi` and `dcgm_prometheus` sources are implemented but only a fake source was exercised (no GPU here).

**Phase 6 — Reports.** `decision_v1`, bootstrap CIs (1000 seeded resamples), weights re-rank live in the UI and persist via `PATCH /reports/{id}`; MD/HTML/JSON/CSV exporters.
Markdown charts are exported as data tables (GitHub cannot render them); the HTML export carries inline SVG charts.

**Phase 7 — Hardening.** Audit log on mutations, RBAC (viewer < annotator < reviewer < admin), CLI, Alembic migration `0001` (verified against SQLite, `alembic check` clean; **Postgres not run here**),
Dockerfile + Compose (not built here — no Docker daemon), `contrib/seed_demo.py`, `contrib/backup.sh`.
