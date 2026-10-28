from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.api import schemas as S
from mizan.api.deps import annotator, container, current_user, get_db, reviewer
from mizan.core.services import annotation as svc
from mizan.core.services.common import Actor

router = APIRouter(tags=["annotation"])


@router.post("/datasets/{dataset_id}/claims", status_code=201)
async def claim(dataset_id: str, body: S.ClaimIn, actor: Actor = annotator, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.claim_batch(c, db, actor, dataset_id, body.minutes, body.strategy, body.dialect)


@router.get("/datasets/{dataset_id}/claims/mine")
async def my_claims(dataset_id: str, actor: Actor = Depends(current_user), c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.my_claims(c, db, actor, dataset_id)


@router.delete("/claims/{segment_id}")
async def release(segment_id: str, actor: Actor = annotator, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.release(c, db, actor, segment_id)


@router.post("/claims/{segment_id}/extend")
async def extend(segment_id: str, minutes: int | None = None, actor: Actor = annotator, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.extend_claim(c, db, actor, segment_id, minutes)


@router.get("/segments/{segment_id}/annotation")
async def get_annotation(segment_id: str, track: str = Query("primary", pattern="^(primary|iaa)$"), actor: Actor = Depends(current_user),
                         db: AsyncSession = Depends(get_db)):
    return await svc.get_working(db, actor, segment_id, track)


@router.put("/segments/{segment_id}/annotation")
async def save_annotation(segment_id: str, body: S.AnnotationIn, track: str = Query("primary", pattern="^(primary|iaa)$"),
                          actor: Actor = annotator, c=Depends(container), db: AsyncSession = Depends(get_db)):
    spans = [s.model_dump() for s in body.spans] if body.spans is not None else None
    return await svc.save(c, db, actor, segment_id, body.text, spans, body.flags, body.note, body.strata, track)


@router.post("/segments/{segment_id}/annotation/submit")
async def submit(segment_id: str, track: str = Query("primary", pattern="^(primary|iaa)$"), actor: Actor = annotator,
                 c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.submit(c, db, actor, segment_id, track)


@router.post("/segments/{segment_id}/annotation/review")
async def review(segment_id: str, body: S.ReviewIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    spans = [s.model_dump() for s in body.spans] if body.spans is not None else None
    return await svc.review(c, db, actor, segment_id, body.action, body.note, body.text, spans)


@router.post("/segments/{segment_id}/annotation/reopen")
async def reopen(segment_id: str, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.reopen(c, db, actor, segment_id)


@router.get("/review/queue")
async def review_queue(dataset: str | None = None, reviewer_: str | None = Query(None, alias="reviewer"), annotator_id: str | None = None,
                       dialect: str | None = None, flag: str | None = None, has_comments: bool | None = None,
                       limit: int = 50, cursor: str | None = None, actor: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    # `reviewer=me` is accepted for spec compatibility: the queue is always "submitted items not authored by me".
    return await svc.review_queue(db, actor, dataset, annotator_id, dialect, flag, has_comments, min(limit, 200), int(cursor or 0))


@router.get("/review/author-stats/{user_id}")
async def author_stats(user_id: str, _: Actor = reviewer, db: AsyncSession = Depends(get_db)):
    return await svc.author_stats(db, user_id)


@router.post("/review/batch-approve/plan")
async def batch_plan(body: S.BatchApproveIn, _: Actor = reviewer):
    return {"spot_check_ids": svc.spot_check_ids(body.segment_ids)}


@router.post("/review/batch-approve")
async def batch_approve(body: S.BatchApproveIn, actor: Actor = reviewer, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.batch_approve(c, db, actor, body.segment_ids, body.listened_ids)


@router.post("/annotations/{annotation_id}/comments", status_code=201)
async def add_comment(annotation_id: str, body: S.CommentIn, actor: Actor = annotator, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.add_comment(c, db, actor, annotation_id, body.body, body.parent_id)


@router.patch("/comments/{comment_id}")
async def patch_comment(comment_id: str, body: S.CommentPatch, actor: Actor = annotator, c=Depends(container), db: AsyncSession = Depends(get_db)):
    return await svc.patch_comment(c, db, actor, comment_id, body.resolved, body.body)
