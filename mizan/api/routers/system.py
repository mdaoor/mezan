from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan import __version__
from mizan.api.deps import admin, container, current_user, get_db
from mizan.core import models as m
from mizan.core.errors import NotFound
from mizan.core.services.common import Actor
from mizan.plugins import registry as plugins
from mizan.plugins.metrics.core import METRIC_PRESETS

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(c=Depends(container)):
    async with c.session() as db:
        await db.execute(select(1))
    return {"ok": True, "version": __version__}


@router.get("/strategies")
async def strategies(_: Actor = Depends(current_user)):
    """Every pluggable strategy with its JSON Schema: the UI renders forms from this (N1)."""
    def pack(reg, **extra):
        return [{"kind": k, "version": c.version, "description": (c.__doc__ or "").strip().splitlines()[0] if c.__doc__ else "",
                 "config_schema": c.config_schema() if hasattr(c, "config_schema") else {}, **{n: getattr(c, n, None) for n in extra}}
                for k, c in reg.items()]
    return {
        "segmenters": pack(plugins.segmenters), "normalizers": pack(plugins.normalizers),
        "exporters": [{"kind": k, "extension": c.extension} for k, c in plugins.exporters.items()],
        "telemetry_sources": [{"kind": k} for k in plugins.telemetry_sources.keys()],
        "metrics": [{"id": k, "version": c.version, "needs_reference": c.needs_reference, "lower_is_better": c.lower_is_better}
                    for k, c in plugins.metrics.items()],
        "metric_presets": METRIC_PRESETS,
    }


@router.get("/audit")
async def audit_log(entity: str | None = None, limit: int = 100, _: Actor = admin, db: AsyncSession = Depends(get_db)):
    q = select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(min(limit, 500))
    if entity:
        q = q.where(m.AuditLog.entity == entity)
    return {"items": [{"id": a.id, "actor_id": a.actor_id, "action": a.action, "entity": a.entity, "entity_id": a.entity_id,
                       "diff": a.diff, "ts": a.ts} for a in (await db.execute(q)).scalars()]}


@router.get("/jobs/{job_id}")
async def job(job_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    j = await db.get(m.Job, job_id)
    if j is None:
        raise NotFound("job not found")
    return {"id": j.id, "kind": j.kind, "status": j.status, "progress": {"done": j.progress_done, "total": j.progress_total},
            "result": j.result, "error": j.error, "created_at": j.created_at, "finished_at": j.finished_at}
