from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api import schemas as S
from mizan.api.deps import container, current_user, get_db, reviewer
from mizan.core import models as m
from mizan.core.jobs import wake
from mizan.core.services import runs as svc
from mizan.core.services.common import Actor, must_get, page_params
from mizan.plugins import registry as plugins
from mizan.plugins.base import ReportModel
import io

router = APIRouter(tags=["runs"])


@router.post("/runs", status_code=202)
async def create_run(body: S.RunIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    run = await svc.create_run(c, db, actor, body.model_dump())
    await db.commit()
    wake(c)
    return {"job_id": run.id, "run_id": run.id, "status": run.status}


@router.post("/runs/batch", status_code=202)
async def create_runs(body: S.RunMultiIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    """New-run wizard: one run per selected endpoint with shared settings."""
    base = (body.run or S.RunIn(model_endpoint_id=body.model_endpoint_ids[0])).model_dump()
    out = []
    for eid in body.model_endpoint_ids:
        run = await svc.create_run(c, db, actor, {**base, "model_endpoint_id": eid, "name": None})
        out.append({"run_id": run.id, "model_endpoint_id": eid})
    await db.commit()
    wake(c)
    return {"runs": out}


@router.get("/runs")
async def list_runs(project_id: str | None = None, type: str | None = None, status: str | None = None, endpoint_id: str | None = None,
                    limit: int | None = None, cursor: str | None = None, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    lim, off = page_params(limit, cursor, 50, 200)
    q = select(m.Run).order_by(m.Run.created_at.desc())
    for col, v in ((m.Run.project_id, project_id), (m.Run.type, type), (m.Run.status, status), (m.Run.model_endpoint_id, endpoint_id)):
        if v:
            q = q.where(col == v)
    rows = (await db.execute(q.offset(off).limit(lim + 1))).scalars().all()
    items = []
    for r in rows[:lim]:
        d = svc.run_view(r)
        d["summary"] = {x["metric_id"]: x["value"] for x in await svc.summaries(db, r.id) if x["stratum_key"] == "overall"} if r.status == "succeeded" else {}
        items.append(d)
    return {"items": items, "next_cursor": str(off + lim) if len(rows) > lim else None}


# fixed paths before /runs/{id}
@router.get("/runs/compare")
async def compare(ids: str, stratum: str | None = None, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await svc.compare(db, [i for i in ids.split(",") if i], stratum)


@router.get("/runs/disagreements")
async def disagreements(ids: str, metric: str = "wer", top: int = Query(100, le=1000), _: Actor = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):
    return await svc.disagreements(db, [i for i in ids.split(",") if i], metric, top)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await svc.run_detail(db, run_id)


@router.post("/runs/{run_id}/{action}")
async def control(run_id: str, action: str, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    if action == "rerun":
        run = await svc.rerun_missing(c, db, actor, run_id)
        return {"run_id": run.id, "status": run.status}
    run = await svc.control(c, db, actor, run_id, action)
    return {"run_id": run.id, "status": run.status}


@router.get("/runs/{run_id}/hypotheses")
async def hypotheses(run_id: str, cursor: str | None = None, limit: int | None = None, q: str | None = None,
                     errors_only: bool = False, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    lim, off = page_params(limit, cursor, 50, 500)
    return await svc.hypotheses_page(db, run_id, lim, off, q, errors_only)


@router.get("/runs/{run_id}/hypotheses/export")
async def export_hypotheses(run_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    recs, off = [], 0
    while True:
        page = await svc.hypotheses_page(db, run_id, 500, off)
        recs += [{k: h[k] for k in ("segment_id", "start_s", "end_s", "text", "latency_ms", "error")} for h in page["items"]]
        if not page["next_cursor"]:
            break
        off = int(page["next_cursor"])
    buf = io.BytesIO()
    mime = plugins.exporters.get("jsonl_dataset")().export(ReportModel(title="hypotheses", meta={"records": recs}), buf)
    return Response(buf.getvalue(), media_type=mime, headers={"Content-Disposition": f'attachment; filename="run-{run_id[:8]}.jsonl"'})


@router.get("/runs/{run_id}/scores")
async def scores(run_id: str, stratum: str | None = None, metric: str | None = None, _: Actor = Depends(current_user),
                 db: AsyncSession = Depends(get_db)):
    return await svc.scores_query(db, run_id, stratum, metric)


@router.get("/runs/{run_id}/segment-scores")
async def segment_scores(run_id: str, metric: str = "wer", limit: int | None = None, cursor: str | None = None, worst_first: bool = True,
                         _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    lim, off = page_params(limit, cursor, 50, 500)
    return await svc.segment_scores_page(db, run_id, metric, lim, off, worst_first)


@router.get("/runs/{run_id}/resolved-config")
async def resolved_config(run_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    r = await must_get(db, m.Run, run_id, "Run")
    return r.resolved_config
