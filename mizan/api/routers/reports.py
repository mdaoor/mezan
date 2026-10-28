import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api import schemas as S
from mizan.api.deps import container, current_user, get_db, reviewer
from mizan.core import models as m
from mizan.core.errors import Conflict, Invalid, NotFound
from mizan.core.jobs import wake
from mizan.core.services import reports as svc
from mizan.core.services.common import Actor, audit, must_get
from mizan.plugins import registry as plugins

router = APIRouter(tags=["reports"])


@router.post("/campaigns", status_code=201)
async def create_campaign(body: S.CampaignIn, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    cp = await svc.create_campaign(db, actor, body.project_id, body.name, body.question, body.run_ids, body.load_run_ids)
    await db.commit()
    return await svc.campaign_view(db, cp)


@router.get("/campaigns")
async def list_campaigns(project_id: str | None = None, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    q = select(m.Campaign).order_by(m.Campaign.created_at.desc())
    if project_id:
        q = q.where(m.Campaign.project_id == project_id)
    return {"items": [await svc.campaign_view(db, cp) for cp in (await db.execute(q)).scalars()]}


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await svc.campaign_view(db, await must_get(db, m.Campaign, campaign_id, "Campaign"))


@router.patch("/campaigns/{campaign_id}")
async def patch_campaign(campaign_id: str, body: S.CampaignPatch, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    cp = await must_get(db, m.Campaign, campaign_id, "Campaign")
    for f in ("name", "question", "status"):
        if getattr(body, f) is not None:
            setattr(cp, f, getattr(body, f))
    await svc.attach(db, cp, body.add_run_ids, body.add_load_run_ids)
    audit(db, actor, "campaign.patch", "campaign", cp.id)
    await db.commit()
    return await svc.campaign_view(db, cp)


@router.post("/campaigns/{campaign_id}/reports", status_code=202)
async def generate(campaign_id: str, body: S.ReportIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    cp = await must_get(db, m.Campaign, campaign_id, "Campaign")
    rep = await svc.request_report(c, db, actor, cp, body.template, body.options)
    await db.commit()
    wake(c)
    return {"job_id": rep.id, "report_id": rep.id, "version": rep.version}


@router.get("/campaigns/{campaign_id}/reports")
async def list_reports(campaign_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(m.Report).where(m.Report.campaign_id == campaign_id).order_by(m.Report.version.desc()))).scalars()
    return {"items": [{"id": r.id, "version": r.version, "template": r.template, "status": r.status, "generated_at": r.generated_at,
                       "error": r.error} for r in rows]}


def _report_view(r: m.Report) -> dict:
    return {"id": r.id, "campaign_id": r.campaign_id, "version": r.version, "template": r.template, "status": r.status,
            "error": r.error, "generated_at": r.generated_at, "artifacts": r.artifacts, **(r.sections or {})}


@router.get("/reports/{report_id}")
async def get_report(report_id: str, _: Actor = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return _report_view(await must_get(db, m.Report, report_id, "Report"))


@router.patch("/reports/{report_id}")
async def patch_report(report_id: str, body: S.ReportPatch, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    """Edit the executive summary and/or decision-matrix weights; changes persist to this report version."""
    r = await must_get(db, m.Report, report_id, "Report")
    if r.status != "ready":
        raise Conflict("report is not ready")
    doc = dict(r.sections)
    if body.weights is not None:
        doc = svc.apply_weights(doc, body.weights)
    if body.summary_text is not None:
        doc["summary_text"], doc["summary_edited"] = body.summary_text, True
        next(s for s in doc["sections"] if s["id"] == "summary")["blocks"][0] = {"type": "paragraph", "text": body.summary_text}
    r.sections = doc
    r.artifacts = []  # cached exports are stale
    audit(db, actor, "report.patch", "report", r.id)
    await db.commit()
    return _report_view(r)


@router.get("/reports/{report_id}/export")
async def export(report_id: str, format: str = Query("md", pattern="^(md|html|json|csv)$"), _: Actor = Depends(current_user),
                 c=Depends(container), db: AsyncSession = Depends(get_db)):
    r = await must_get(db, m.Report, report_id, "Report")
    if r.status != "ready":
        raise Conflict(f"report is {r.status}")
    kind = {"md": "markdown", "html": "html", "json": "json", "csv": "csv"}[format]
    buf = io.BytesIO()
    mime = plugins.exporters.get(kind)().export(svc.to_model(r), buf)
    cp = await db.get(m.Campaign, r.campaign_id)
    key = f"projects/{cp.project_id}/reports/{r.id}/report.{format}"
    c.blobs.put_bytes(key, buf.getvalue())
    if key not in [a.get("key") for a in r.artifacts]:
        r.artifacts = [*r.artifacts, {"format": format, "key": key}]
        await db.commit()
    return Response(buf.getvalue(), media_type=mime, headers={"Content-Disposition": f'attachment; filename="report-v{r.version}.{format}"'})
