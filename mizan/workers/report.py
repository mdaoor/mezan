from __future__ import annotations

from mizan.core.jobs import handler


@handler("report")
async def build(c, job, ctx) -> dict:
    from mizan.core.services import reports

    await reports.build_and_store(c, job.payload["report_id"])
    return {"report_id": job.payload["report_id"]}
