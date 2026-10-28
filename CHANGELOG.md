# Changelog

## 1.0.0
Initial implementation of the handover spec (phases 0–7).

### Schema additions beyond spec §7.1 (each is in migration `0001`)
- `users.leaderboard_opt_in` — opt-in leaderboard (§9.8).
- `annotations.track` (`primary`|`iaa`), `annotations.note` — blind double annotation for IAA (F7); the unintelligible note (§9.3).
- `gold_items.flags` — frozen segment flags so `hallucination_rate` is reproducible from the gold set alone.
- `model_endpoints.last_health`, `last_health_at` — "last health check" on endpoint cards (§9.5).
- `jobs` table — durable queue + job ids for every 202 response (§6).
- `runs.error`, `runs.created_at`; `reports.status|error|options`; `segment_scores.applicable`.
- `load_runs.dataset_id|telemetry_source|info|created_by` (in §7.1 but without the aborted-reason/saturation `info` JSON); `load_samples`/`telemetry_samples` get an integer `id` PK; `load_summaries` add `requests`, `error_classes`, `gpu`, `t_start`, `t_end`.
- `audit_log` is written by every mutating action (F16, §10.3 phase 7).

### Contract deviations
- `Metric.score(..., ctx=None)` — extra optional `ctx` carries `latency_ms`/`audio_s`, which the spec's signature has no way to receive (needed for `latency_ms`, `rtfx`).
- `Metric.aggregate_all()` — a metric may emit companion ids (`latency_ms_p50`, `latency_ms_p95`).
- `MetricResult.applicable` — segments a metric does not apply to (no English tokens, not `no_speech`) are excluded from corpus aggregates.
- Extra endpoints the UI needs: `/strategies`, `/model-endpoint-presets`, `/runs/batch`, `/runs/{id}/rerun`, `/review/batch-approve*`, `/claims/{id}/extend`, `/datasets/{id}/claims/mine`, `/datasets/{id}/iaa/sample`, `/segments/{id}/queue`, `PATCH /reports/{id}`, `/campaigns` list/patch, `/audit`.
