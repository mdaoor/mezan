"""Engine / ModelEndpoint registry, connection tests, adapter construction."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import jsonschema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.audio import synth_wav
from mizan.core.errors import Invalid, NotFound
from mizan.core.services.common import must_get
from mizan.plugins import registry as plugins
from mizan.plugins.base import AudioInput, EngineAdapter


def schema_defaults(schema: dict) -> dict:
    out = {}
    for k, v in (schema.get("properties") or {}).items():
        if "default" in v:
            out[k] = v["default"]
    return out


def adapter_capabilities(cls: type[EngineAdapter]) -> dict:
    cfg = schema_defaults(cls.config_schema())
    for k in cls.config_schema().get("required", []):
        cfg.setdefault(k, "x")
    try:
        return cls(cfg).capabilities().to_dict()
    except Exception:
        return {}


def build_config(kind: str, model_id: str, base_url: str, config: dict | None) -> dict:
    cls = plugins.adapters.get(kind)
    schema = cls.config_schema()
    cfg = {**schema_defaults(schema), **(config or {}), "model": model_id}
    if base_url:
        cfg["base_url"] = base_url
    try:
        jsonschema.validate(cfg, schema)
    except jsonschema.ValidationError as e:
        raise Invalid(f"config invalid: {e.message}", path=list(e.path)) from e
    return cfg


async def engine_kind(db: AsyncSession, engine_id: str) -> str:
    return (await must_get(db, m.Engine, engine_id, "Engine")).kind


async def adapter_for(db: AsyncSession, ep: m.ModelEndpoint) -> EngineAdapter:
    kind = await engine_kind(db, ep.engine_id)
    cfg = {**ep.config, "model": ep.model_id, **({"base_url": ep.base_url} if ep.base_url else {})}
    return plugins.adapters.get(kind)(cfg)


async def create_endpoint(db: AsyncSession, engine_kind_: str, model_id: str, display_name: str,
                          base_url: str, config: dict, tags: list[str]) -> m.ModelEndpoint:
    if not plugins.adapters.has(engine_kind_):
        raise Invalid(f"unknown engine kind '{engine_kind_}'", available=plugins.adapters.keys())
    eng = (await db.execute(select(m.Engine).where(m.Engine.kind == engine_kind_))).scalar_one_or_none()
    if eng is None:
        raise NotFound(f"engine '{engine_kind_}' not synced")
    cfg = build_config(engine_kind_, model_id, base_url, config)
    ep = m.ModelEndpoint(engine_id=eng.id, model_id=model_id, display_name=display_name,
                         base_url=cfg.get("base_url", base_url), config=cfg, tags=tags)
    db.add(ep)
    await db.flush()
    return ep


def public_endpoint(ep: m.ModelEndpoint, eng: m.Engine | None = None) -> dict:
    cfg = {k: ("***" if k in ("api_key", "secret") and v not in ("", "EMPTY", None) else v) for k, v in ep.config.items()}
    d = {"id": ep.id, "engine_id": ep.engine_id, "model_id": ep.model_id, "display_name": ep.display_name,
         "base_url": ep.base_url, "config": cfg, "tags": ep.tags, "status": ep.status,
         "last_health": ep.last_health, "last_health_at": ep.last_health_at}
    if eng:
        d.update(engine_kind=eng.kind, external=eng.external, capabilities=eng.capabilities)
    return d


async def test_endpoint(c, db: AsyncSession, ep: m.ModelEndpoint, segment_id: str | None = None) -> dict:
    """Health + a one-segment smoke test (a gold segment if available, else 2 s of synthetic tone)."""
    from mizan.core.services.segaudio import SegmentAudioReader

    adapter = await adapter_for(db, ep)
    try:
        health = await adapter.health()
        source, audio, ref = "synthetic", None, None
        if segment_id is None:
            row = (await db.execute(
                select(m.GoldItem.segment_id, m.GoldItem.text).limit(1))).first()
            segment_id, ref = (row[0], row[1]) if row else (None, None)
        if segment_id:
            seg = await db.get(m.Segment, segment_id)
            if seg:
                reader = SegmentAudioReader(c)
                await reader.prepare(db, [seg])
                audio = AudioInput(await reader.wav(seg), seg.duration_s, seg.id)
                source = f"segment:{seg.id}"
        if audio is None:
            audio = AudioInput(synth_wav(2.0, 440), 2.0, "smoke-test")
        smoke: dict = {"source": source}
        t0 = time.perf_counter()
        try:
            res = await adapter.transcribe(audio, {"timeout_s": 60})
            smoke.update(ok=True, text=res.text, latency_ms=res.latency_ms, reference=ref)
        except Exception as e:
            smoke.update(ok=False, error=getattr(e, "error_class", type(e).__name__), detail=str(e)[:300],
                         latency_ms=(time.perf_counter() - t0) * 1000)
        ok = bool(health.get("ok")) and smoke.get("ok", False)
        ep.status, ep.last_health, ep.last_health_at = ("ok" if ok else "error"), health, datetime.now(timezone.utc)
        await db.commit()
        return {"health": health, "smoke": smoke, "ok": ok}
    finally:
        await adapter.aclose()
