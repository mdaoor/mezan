"""Request bodies. ``examples`` feed the OpenAPI docs (DoD: every endpoint has an example)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Body):
    email: str
    password: str
    model_config = ConfigDict(json_schema_extra={"examples": [{"email": "admin@mizan.local", "password": "admin"}]})


class ProjectIn(Body):
    name: str
    description: str = ""
    settings: dict = {}
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "Whisper replacement Q4", "description": "Arabic + English call audio"}]})


class DatasetIn(Body):
    name: str
    description: str = ""
    language_hints: list[str] = []
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "support-calls-batch-1", "language_hints": ["ar", "en"]}]})


class SegmenterSpec(Body):
    kind: str = "energy_vad"
    config: dict = {}


class SegmentIn(Body):
    segmenter: SegmenterSpec = SegmenterSpec()
    audio_file_ids: list[str] | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"segmenter": {"kind": "energy_vad", "config": {"max_seg_s": 25}}}]})


class BootstrapIn(Body):
    model_endpoint_id: str
    options: dict = {}
    name: str | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"model_endpoint_id": "<endpoint id>", "options": {"concurrency": 48, "language_hint": "ar"}}]})


class SegmentPatch(Body):
    strata: dict | None = None
    flags: list[str] | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"strata": {"dialect": "gulf"}, "flags": ["overlap"]}]})


class ClaimIn(Body):
    minutes: float = Field(10, gt=0, le=240)
    strategy: Literal["oldest_unclaimed", "random", "my_dialect", "iaa"] = "oldest_unclaimed"
    dialect: str | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"minutes": 10, "strategy": "oldest_unclaimed"}]})


class SpanIn(Body):
    start_char: int
    end_char: int
    lang: Literal["ar", "en", "other"]


class AnnotationIn(Body):
    text: str
    spans: list[SpanIn] | None = None
    flags: list[str] | None = None
    note: str | None = None
    strata: dict | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"text": "انا عندي meeting بكره", "flags": [], "strata": {"dialect": "egyptian"}}]})


class ReviewIn(Body):
    action: Literal["approve", "request_changes", "start"]
    note: str | None = None
    text: str | None = None
    spans: list[SpanIn] | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"action": "request_changes", "note": "second sentence is missing"}]})


class BatchApproveIn(Body):
    segment_ids: list[str]
    listened_ids: list[str] = []
    model_config = ConfigDict(json_schema_extra={"examples": [{"segment_ids": ["<segment id>"], "listened_ids": ["<segment id>"]}]})


class CommentIn(Body):
    body: str
    parent_id: str | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"body": "@sara can you check the English term?"}]})


class CommentPatch(Body):
    resolved: bool | None = None
    body: str | None = None


class GoldSetIn(Body):
    name: str
    policy: dict = {}
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "gold-q4", "policy": {"require_review": True, "min_approvals": 1, "exclude_flags": ["unintelligible"], "iaa_sample_pct": 5}}]})


class IAASampleIn(Body):
    pct: float = Field(5, gt=0, le=100)
    seed: int = 0


class EndpointIn(Body):
    engine_kind: str
    model_id: str
    display_name: str
    base_url: str = ""
    config: dict = {}
    tags: list[str] = []
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "engine_kind": "vllm_openai", "model_id": "IFM/Jais-ASR", "display_name": "Jais-ASR (vLLM GPU0)",
        "base_url": "http://vllm-gpu0:8000", "config": {"max_concurrency": 48}}]})


class EndpointPatch(Body):
    display_name: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    config: dict | None = None
    tags: list[str] | None = None


class EndpointTestIn(Body):
    segment_id: str | None = None


class RefIn(Body):
    type: Literal["gold_set", "run", "none"] = "none"
    id: str | None = None


class NormalizerIn(Body):
    kind: str = "arabic_v1"
    config: dict = {}


class RunIn(Body):
    type: Literal["evaluation", "agreement", "bootstrap"] = "evaluation"
    name: str | None = None
    model_endpoint_id: str
    reference: RefIn = RefIn()
    dataset_id: str | None = None
    segmenter: dict | None = None
    normalizer: NormalizerIn = NormalizerIn()
    metric_set: str | list[str] = "arabic_codeswitch_default"
    options: dict[str, Any] = {}
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "type": "evaluation", "name": "jais vs gold v1", "model_endpoint_id": "<endpoint id>",
        "reference": {"type": "gold_set", "id": "<gold set id>"}, "normalizer": {"kind": "arabic_v1"},
        "metric_set": "arabic_codeswitch_default", "options": {"concurrency": 48, "language_hint": "ar", "timeout_s": 120}}]})


class RunMultiIn(Body):
    """Wizard shortcut: one run per endpoint."""
    model_endpoint_ids: list[str]
    run: RunIn | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"model_endpoint_ids": ["<endpoint a>", "<endpoint b>"], "run": {
        "type": "evaluation", "model_endpoint_id": "<ignored>", "reference": {"type": "gold_set", "id": "<gold set id>"}}}]})


class LoadScenarioIn(Body):
    name: str
    concurrency_steps: list[int] = [1, 4, 16, 32, 64]
    step_duration_s: float = 120
    length_buckets: dict[str, float] = {"5-15s": 0.6, "15-30s": 0.4}
    warmup_s: float = 30
    timeout_s: float = 120
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "ramp-1-64", "concurrency_steps": [1, 4, 16, 32, 64], "step_duration_s": 120,
                                                                "length_buckets": {"5-15s": 0.6, "15-30s": 0.4}, "warmup_s": 30, "timeout_s": 120}]})


class LoadRunIn(Body):
    model_endpoint_id: str
    scenario_id: str
    dataset_id: str
    telemetry_source: str | None = None
    model_config = ConfigDict(json_schema_extra={"examples": [{"model_endpoint_id": "<endpoint id>", "scenario_id": "<scenario id>",
                                                                "dataset_id": "<dataset id>", "telemetry_source": "nvidia_smi"}]})


class CampaignIn(Body):
    project_id: str
    name: str
    question: str = ""
    run_ids: list[str] = []
    load_run_ids: list[str] = []
    model_config = ConfigDict(json_schema_extra={"examples": [{"project_id": "<project id>", "name": "Replace Whisper, Q4",
                                                                "question": "Replace Whisper for Arabic+English?", "run_ids": [], "load_run_ids": []}]})


class CampaignPatch(Body):
    name: str | None = None
    question: str | None = None
    add_run_ids: list[str] = []
    add_load_run_ids: list[str] = []
    status: Literal["open", "closed"] | None = None


class ReportIn(Body):
    template: str = "decision_v1"
    options: dict[str, Any] = {}
    model_config = ConfigDict(json_schema_extra={"examples": [{"template": "decision_v1", "options": {"weights": {"accuracy": 0.5, "code_switch": 0.2, "throughput": 0.2, "robustness": 0.1}}}]})


class ReportPatch(Body):
    summary_text: str | None = None
    weights: dict[str, float] | None = None


class UserIn(Body):
    email: str
    name: str
    role: Literal["admin", "reviewer", "annotator", "viewer"] = "annotator"
    password: str
    model_config = ConfigDict(json_schema_extra={"examples": [{"email": "sara@example.com", "name": "Sara", "role": "reviewer", "password": "change-me"}]})


class UserPatch(Body):
    name: str | None = None
    role: Literal["admin", "reviewer", "annotator", "viewer"] | None = None
    password: str | None = None
    leaderboard_opt_in: bool | None = None


class QueueIn(Body):
    priority: int = 1
