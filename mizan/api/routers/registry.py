from pathlib import Path

import yaml
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api import schemas as S
from mizan.api.deps import container, current_user, get_db, reviewer
from mizan.core import models as m
from mizan.core.errors import Conflict, Invalid
from mizan.core.services import registry as svc
from mizan.core.services.common import Actor, audit, must_get
from mizan.plugins import registry as plugins

router = APIRouter(tags=["registry"])


@router.get("/engines")
async def engines(_: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(m.Engine).order_by(m.Engine.kind))).scalars()
    return {"items": [{"id": e.id, "kind": e.kind, "name": e.name, "version": e.version, "external": e.external,
                       "capabilities": e.capabilities,
                       "config_schema": plugins.adapters.get(e.kind).config_schema() if plugins.adapters.has(e.kind) else {}}
                      for e in rows]}


@router.get("/model-endpoint-presets")
async def presets(c=Depends(container), _: Actor = Depends(current_user)):
    for p in (Path("plugins/presets.yaml"), Path(c.settings.plugins_file).with_name("presets.yaml")):
        if p.exists():
            return {"items": yaml.safe_load(p.read_text()) or []}
    return {"items": []}


@router.post("/model-endpoints", status_code=201)
async def create_endpoint(body: S.EndpointIn, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    ep = await svc.create_endpoint(db, body.engine_kind, body.model_id, body.display_name, body.base_url, body.config, body.tags)
    audit(db, actor, "endpoint.create", "model_endpoint", ep.id, {"kind": body.engine_kind})
    await db.commit()
    return svc.public_endpoint(ep, await db.get(m.Engine, ep.engine_id))


@router.get("/model-endpoints")
async def list_endpoints(_: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    engs = {e.id: e for e in (await db.execute(select(m.Engine))).scalars()}
    rows = (await db.execute(select(m.ModelEndpoint).order_by(m.ModelEndpoint.created_at))).scalars()
    return {"items": [svc.public_endpoint(e, engs.get(e.engine_id)) for e in rows]}


@router.get("/model-endpoints/{endpoint_id}")
async def get_endpoint(endpoint_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    ep = await must_get(db, m.ModelEndpoint, endpoint_id, "ModelEndpoint")
    return svc.public_endpoint(ep, await db.get(m.Engine, ep.engine_id))


@router.patch("/model-endpoints/{endpoint_id}")
async def patch_endpoint(endpoint_id: str, body: S.EndpointPatch, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    ep = await must_get(db, m.ModelEndpoint, endpoint_id, "ModelEndpoint")
    eng = await db.get(m.Engine, ep.engine_id)
    if body.display_name is not None:
        ep.display_name = body.display_name
    if body.tags is not None:
        ep.tags = body.tags
    if any(x is not None for x in (body.base_url, body.model_id, body.config)):
        merged = {**ep.config, **(body.config or {})}
        for k in ("api_key", "secret"):  # masked values from GET must not overwrite the real secret
            if merged.get(k) == "***":
                merged[k] = ep.config.get(k)
        model_id = body.model_id or ep.model_id
        base_url = body.base_url if body.base_url is not None else ep.base_url
        ep.config = svc.build_config(eng.kind, model_id, base_url, merged)
        ep.model_id, ep.base_url, ep.status = model_id, ep.config.get("base_url", base_url), "unknown"
    audit(db, actor, "endpoint.patch", "model_endpoint", ep.id)
    await db.commit()
    return svc.public_endpoint(ep, eng)


@router.delete("/model-endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: str, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    ep = await must_get(db, m.ModelEndpoint, endpoint_id, "ModelEndpoint")
    used = (await db.execute(select(func.count()).select_from(m.Run).where(m.Run.model_endpoint_id == ep.id))).scalar() \
        + (await db.execute(select(func.count()).select_from(m.LoadRun).where(m.LoadRun.model_endpoint_id == ep.id))).scalar()
    if used:
        raise Conflict(f"endpoint is referenced by {used} run(s); runs are append-only records, so it cannot be deleted")
    audit(db, actor, "endpoint.delete", "model_endpoint", ep.id)
    await db.delete(ep)
    await db.commit()


@router.post("/model-endpoints/{endpoint_id}/test")
async def test_endpoint(endpoint_id: str, body: S.EndpointTestIn | None = None, _: Actor = reviewer, c=Depends(container),
                        db: AsyncSession = Depends(get_db)):
    ep = await must_get(db, m.ModelEndpoint, endpoint_id, "ModelEndpoint")
    return await svc.test_endpoint(c, db, ep, body.segment_id if body else None)


@router.get("/model-endpoints/{endpoint_id}/models")
async def endpoint_models(endpoint_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    ep = await must_get(db, m.ModelEndpoint, endpoint_id, "ModelEndpoint")
    adapter = await svc.adapter_for(db, ep)
    try:
        return {"models": await adapter.list_models()}
    except Exception as e:
        raise Invalid(f"could not list models: {type(e).__name__}: {e}") from e
    finally:
        await adapter.aclose()
