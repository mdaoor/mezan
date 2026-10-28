"""SQLAlchemy model — spec section 7.1. Deviations from the spec are listed in CHANGELOG.md."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from mizan.core.db import Base, utcnow


def uid() -> str:
    return str(uuid.uuid4())


def pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=uid)


def fk(target: str, **kw) -> Mapped[str]:
    return mapped_column(String(36), ForeignKey(target, ondelete=kw.pop("ondelete", "CASCADE")), **kw)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk()
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="annotator")  # admin|reviewer|annotator|viewer
    password_hash: Mapped[str] = mapped_column(String(255))
    leaderboard_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    settings: Mapped[dict] = mapped_column(default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = fk("projects.id")
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    language_hints: Mapped[list] = mapped_column(default=list)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AudioFile(Base):
    __tablename__ = "audio_files"
    __table_args__ = (UniqueConstraint("dataset_id", "sha256"),)
    id: Mapped[str] = pk()
    dataset_id: Mapped[str] = fk("datasets.id")
    original_blob: Mapped[str] = mapped_column(String(512))
    normalized_blob: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|processing|ready|failed
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("audio_file_id", "idx"),
                      Index("ix_segments_strata_gin", "strata", postgresql_using="gin"))
    id: Mapped[str] = pk()
    audio_file_id: Mapped[str] = fk("audio_files.id")
    idx: Mapped[int] = mapped_column(Integer)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    segmenter_kind: Mapped[str] = mapped_column(String(64), default="")
    segmenter_version: Mapped[str] = mapped_column(String(32), default="")
    strata: Mapped[dict] = mapped_column(default=dict)
    flags: Mapped[list] = mapped_column(default=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (UniqueConstraint("segment_id", "version"),
                      Index("ix_ann_seg_status", "segment_id", "status"),
                      Index("ix_ann_claim", "claimed_by", "claim_expires_at"))
    id: Mapped[str] = pk()
    segment_id: Mapped[str] = fk("segments.id")
    version: Mapped[int] = mapped_column(Integer)
    track: Mapped[str] = mapped_column(String(10), default="primary")  # primary | iaa (double annotation)
    text: Mapped[str] = mapped_column(Text, default="")
    spans: Mapped[list] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    source: Mapped[str] = mapped_column(String(80), default="human")
    note: Mapped[str] = mapped_column(Text, default="")
    author_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[str] = pk()
    annotation_id: Mapped[str] = fk("annotations.id")
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class GoldSet(Base):
    __tablename__ = "gold_sets"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)
    id: Mapped[str] = pk()
    dataset_id: Mapped[str] = fk("datasets.id")
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    frozen_at: Mapped[datetime] = mapped_column(default=utcnow)
    policy: Mapped[dict] = mapped_column(default=dict)


class GoldItem(Base):
    __tablename__ = "gold_items"
    gold_set_id: Mapped[str] = mapped_column(String(36), ForeignKey("gold_sets.id", ondelete="CASCADE"), primary_key=True)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True)
    annotation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    spans: Mapped[list] = mapped_column(default=list)
    strata: Mapped[dict] = mapped_column(default=dict)
    flags: Mapped[list] = mapped_column(default=list)  # frozen copy: hallucination metrics need no_speech flags


class Engine(Base):
    __tablename__ = "engines"
    id: Mapped[str] = pk()
    kind: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(32))
    capabilities: Mapped[dict] = mapped_column(default=dict)
    external: Mapped[bool] = mapped_column(Boolean, default=False)


class ModelEndpoint(Base):
    __tablename__ = "model_endpoints"
    id: Mapped[str] = pk()
    engine_id: Mapped[str] = fk("engines.id", ondelete="RESTRICT")
    model_id: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(String(512), default="")
    config: Mapped[dict] = mapped_column(default=dict)
    tags: Mapped[list] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown|ok|error
    last_health: Mapped[dict] = mapped_column(default=dict)
    last_health_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    """Durable queue for long operations (ingest, segment, run, load-run, report, import). Rows double as job ids."""
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status", "status", "created_at"),)
    id: Mapped[str] = pk()
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed|cancelled
    result: Mapped[dict] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = fk("projects.id")
    type: Mapped[str] = mapped_column(String(16))  # evaluation|agreement|bootstrap
    name: Mapped[str] = mapped_column(String(255))
    model_endpoint_id: Mapped[str] = fk("model_endpoints.id", ondelete="RESTRICT")
    reference_type: Mapped[str] = mapped_column(String(16), default="none")  # gold_set|run|none
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dataset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    segmenter_cfg: Mapped[dict] = mapped_column(default=dict)
    normalizer_cfg: Mapped[dict] = mapped_column(default=dict)
    metric_set: Mapped[list] = mapped_column(default=list)
    options: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_config: Mapped[dict] = mapped_column(default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    __table_args__ = (UniqueConstraint("run_id", "segment_id"),)
    id: Mapped[str] = pk()
    run_id: Mapped[str] = fk("runs.id")
    segment_id: Mapped[str] = fk("segments.id")
    text: Mapped[str] = mapped_column(Text, default="")
    spans: Mapped[list] = mapped_column(default=list)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)


class SegmentScore(Base):
    __tablename__ = "segment_scores"
    __table_args__ = (Index("ix_scores_run_metric", "run_id", "metric_id"),)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    segment_id: Mapped[str] = mapped_column(String(36), ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True)
    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(default=dict)
    applicable: Mapped[bool] = mapped_column(Boolean, default=True)


class RunSummary(Base):
    __tablename__ = "run_summaries"
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    stratum_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    stratum_value: Mapped[str] = mapped_column(String(128), primary_key=True)
    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    n: Mapped[int] = mapped_column(Integer)


class LoadScenario(Base):
    __tablename__ = "load_scenarios"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(255))
    concurrency_steps: Mapped[list] = mapped_column(default=list)
    step_duration_s: Mapped[float] = mapped_column(Float, default=120)
    length_buckets: Mapped[dict] = mapped_column(default=dict)
    warmup_s: Mapped[float] = mapped_column(Float, default=30)
    timeout_s: Mapped[float] = mapped_column(Float, default=120)


class LoadRun(Base):
    __tablename__ = "load_runs"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = fk("projects.id")
    model_endpoint_id: Mapped[str] = fk("model_endpoints.id", ondelete="RESTRICT")
    scenario_id: Mapped[str] = fk("load_scenarios.id", ondelete="RESTRICT")
    dataset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    telemetry_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    info: Mapped[dict] = mapped_column(default=dict)  # aborted_reason, resolved scenario, saturation
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LoadSample(Base):
    __tablename__ = "load_samples"
    __table_args__ = (Index("ix_lsamples_run_step", "load_run_id", "step"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    load_run_id: Mapped[str] = fk("load_runs.id")
    ts: Mapped[datetime] = mapped_column()
    step: Mapped[int] = mapped_column(Integer)
    segment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float)
    ok: Mapped[bool] = mapped_column(Boolean)
    error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_s: Mapped[float] = mapped_column(Float, default=0)


class TelemetrySample(Base):
    __tablename__ = "telemetry_samples"
    __table_args__ = (Index("ix_tele_run_ts", "load_run_id", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    load_run_id: Mapped[str] = fk("load_runs.id")
    ts: Mapped[datetime] = mapped_column()
    device: Mapped[str] = mapped_column(String(64))
    util_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_used_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)


class LoadSummary(Base):
    __tablename__ = "load_summaries"
    load_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("load_runs.id", ondelete="CASCADE"), primary_key=True)
    step: Mapped[int] = mapped_column(Integer, primary_key=True)
    concurrency: Mapped[int] = mapped_column(Integer)
    p50: Mapped[float] = mapped_column(Float, default=0)
    p95: Mapped[float] = mapped_column(Float, default=0)
    p99: Mapped[float] = mapped_column(Float, default=0)
    rtfx: Mapped[float] = mapped_column(Float, default=0)
    req_per_s: Mapped[float] = mapped_column(Float, default=0)
    error_rate: Mapped[float] = mapped_column(Float, default=0)
    timeouts: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    error_classes: Mapped[dict] = mapped_column(default=dict)
    gpu: Mapped[dict] = mapped_column(default=dict)  # per-step telemetry aggregate (max util / VRAM)
    t_start: Mapped[datetime | None] = mapped_column(nullable=True)
    t_end: Mapped[datetime | None] = mapped_column(nullable=True)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = fk("projects.id")
    name: Mapped[str] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CampaignRun(Base):
    __tablename__ = "campaign_runs"
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)


class CampaignLoadRun(Base):
    __tablename__ = "campaign_load_runs"
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    load_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("load_runs.id", ondelete="CASCADE"), primary_key=True)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = pk()
    campaign_id: Mapped[str] = fk("campaigns.id")
    version: Mapped[int] = mapped_column(Integer)
    template: Mapped[str] = mapped_column(String(64))
    sections: Mapped[dict] = mapped_column(default=dict)  # {"title","question","header","sections":[...],"decision":{...}}
    artifacts: Mapped[list] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[dict] = mapped_column(default=dict)
    generated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff: Mapped[dict] = mapped_column(default=dict)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
