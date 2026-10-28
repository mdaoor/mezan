from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api import schemas as S
from mizan.api.deps import container, current_user, get_db, reviewer
from mizan.core import models as m
from mizan.core.jobs import wake
from mizan.core.services import loadtest as svc
from mizan.core.services.common import Actor, page_params

router = APIRouter(tags=["load"])


@router.post("/load-scenarios", status_code=201)
async def create_scenario(body: S.LoadScenarioIn, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    sc = await svc.create_scenario(db, actor, body.model_dump())
    await db.commit()
    return svc.scenario_view(sc)


@router.get("/load-scenarios")
async def list_scenarios(_: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return {"items": [svc.scenario_view(s) for s in (await db.execute(select(m.LoadScenario).order_by(m.LoadScenario.name))).scalars()]}


@router.post("/load-runs", status_code=202)
async def create_load_run(body: S.LoadRunIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    lr = await svc.create_load_run(c, db, actor, body.model_dump())
    await db.commit()
    wake(c)
    return {"job_id": lr.id, "load_run_id": lr.id, "status": lr.status}


@router.get("/load-runs")
async def list_load_runs(project_id: str | None = None, endpoint_id: str | None = None, _: Actor = Depends(current_user),
                         db: AsyncSession = Depends(get_db)):
    q = select(m.LoadRun).order_by(m.LoadRun.created_at.desc()).limit(200)
    if project_id:
        q = q.where(m.LoadRun.project_id == project_id)
    if endpoint_id:
        q = q.where(m.LoadRun.model_endpoint_id == endpoint_id)
    out = []
    for lr in (await db.execute(q)).scalars():
        d = await svc.detail(db, lr.id)
        out.append({k: d[k] for k in ("id", "model_endpoint_id", "scenario_id", "dataset_id", "status", "started_at", "finished_at", "saturation", "live")}
                   | {"best_rtfx": max([s["rtfx"] for s in d["steps"]], default=None)})
    return {"items": out}


@router.get("/load-runs/{load_run_id}")
async def get_load_run(load_run_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await svc.detail(db, load_run_id)


@router.post("/load-runs/{load_run_id}/{action}")
async def control(load_run_id: str, action: str, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    lr = await svc.control(c, db, actor, load_run_id, action)
    return {"load_run_id": lr.id, "status": lr.status}


@router.get("/load-runs/{load_run_id}/samples")
async def samples(load_run_id: str, step: int | None = None, limit: int | None = None, cursor: str | None = None,
                  _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    lim, off = page_params(limit, cursor, 1000, 10000)
    return await svc.samples(db, load_run_id, step, lim, off)


@router.get("/load-runs/{load_run_id}/telemetry")
async def telemetry(load_run_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await svc.telemetry(db, load_run_id)
