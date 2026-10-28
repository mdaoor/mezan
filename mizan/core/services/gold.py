"""Gold sets (immutable snapshots) and inter-annotator agreement."""
from __future__ import annotations

import random
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.errors import Conflict, Invalid, NotFound
from mizan.core.services.common import Actor, audit, must_get
from mizan.plugins import registry as plugins
from mizan.plugins.textutil import lang_mix

DEFAULT_POLICY = {"require_review": True, "min_approvals": 1, "exclude_flags": ["unintelligible"], "iaa_sample_pct": 5}


async def freeze(db: AsyncSession, actor: Actor, dataset_id: str, name: str, policy: dict | None = None) -> m.GoldSet:
    await must_get(db, m.Dataset, dataset_id, "Dataset")
    pol = {**DEFAULT_POLICY, **(policy or {})}
    if pol["min_approvals"] != 1:
        raise Invalid("min_approvals > 1 is not supported: the workflow records a single reviewer approval")
    statuses = ["approved"] if pol["require_review"] else ["approved", "submitted", "in_review"]
    latest_v = (select(m.Annotation.segment_id, func.max(m.Annotation.version).label("v"))
                .where(m.Annotation.track == "primary", m.Annotation.status.in_(statuses)).group_by(m.Annotation.segment_id).subquery())
    q = (select(m.Segment, m.Annotation).join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .join(latest_v, latest_v.c.segment_id == m.Segment.id)
         .join(m.Annotation, (m.Annotation.segment_id == m.Segment.id) & (m.Annotation.version == latest_v.c.v))
         .where(m.AudioFile.dataset_id == dataset_id).order_by(m.Segment.audio_file_id, m.Segment.idx))
    excl = set(pol["exclude_flags"])
    rows = [(s, a) for s, a in (await db.execute(q)).all() if not (excl & set(s.flags))]
    if not rows:
        raise Invalid("no approved segments to freeze (after applying policy)")
    ver = ((await db.execute(select(func.max(m.GoldSet.version)).where(m.GoldSet.dataset_id == dataset_id))).scalar() or 0) + 1
    gs = m.GoldSet(dataset_id=dataset_id, version=ver, name=name, policy=pol)
    db.add(gs)
    await db.flush()
    for s, a in rows:
        strata = {**s.strata, "lang_mix": lang_mix(a.text, a.spans)}
        db.add(m.GoldItem(gold_set_id=gs.id, segment_id=s.id, annotation_id=a.id, text=a.text, spans=a.spans,
                          strata=strata, flags=list(s.flags)))
    audit(db, actor, "goldset.freeze", "gold_set", gs.id, {"version": ver, "segments": len(rows)})
    return gs


async def stats(db: AsyncSession, gs: m.GoldSet) -> dict:
    rows = (await db.execute(select(m.GoldItem, m.Segment).join(m.Segment, m.Segment.id == m.GoldItem.segment_id)
                             .where(m.GoldItem.gold_set_id == gs.id))).all()
    dist: dict[str, Counter] = {}
    minutes = 0.0
    for it, seg in rows:
        minutes += (seg.end_s - seg.start_s) / 60
        for k, v in it.strata.items():
            if k in ("priority", "iaa"):
                continue
            dist.setdefault(k, Counter())[str(v)] += 1
    return {"id": gs.id, "dataset_id": gs.dataset_id, "version": gs.version, "name": gs.name, "frozen_at": gs.frozen_at,
            "policy": gs.policy, "segment_count": len(rows), "minutes": round(minutes, 3),
            "strata_distribution": {k: dict(c) for k, c in dist.items()}}


async def get_by_selector(db: AsyncSession, dataset_id: str, selector: str) -> m.GoldSet:
    """'v2' | '2' | gold-set id."""
    s = selector.lower().lstrip("v")
    if s.isdigit():
        gs = (await db.execute(select(m.GoldSet).where(m.GoldSet.dataset_id == dataset_id, m.GoldSet.version == int(s)))).scalar_one_or_none()
    else:
        gs = await db.get(m.GoldSet, selector)
    if gs is None:
        raise NotFound(f"gold set '{selector}' not found")
    return gs


# --------------------------------------------------------------------- IAA
async def mark_iaa_sample(db: AsyncSession, actor: Actor, dataset_id: str, pct: float, seed: int = 0) -> dict:
    """Flag pct% of approved segments for blind double annotation (strata.iaa=true)."""
    latest_v = (select(m.Annotation.segment_id, func.max(m.Annotation.version).label("v"))
                .where(m.Annotation.track == "primary").group_by(m.Annotation.segment_id).subquery())
    q = (select(m.Segment).join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .join(latest_v, latest_v.c.segment_id == m.Segment.id)
         .join(m.Annotation, (m.Annotation.segment_id == m.Segment.id) & (m.Annotation.version == latest_v.c.v))
         .where(m.AudioFile.dataset_id == dataset_id, m.Annotation.status == "approved").order_by(m.Segment.id))
    segs = [s for s in (await db.execute(q)).scalars() if not s.strata.get("iaa")]
    k = max(1, round(len(segs) * pct / 100)) if segs else 0
    pick = random.Random(seed).sample(segs, min(k, len(segs)))
    for s in pick:
        s.strata = {**s.strata, "iaa": True}
    audit(db, actor, "iaa.sample", "dataset", dataset_id, {"pct": pct, "segments": len(pick)})
    return {"marked": len(pick), "segment_ids": [s.id for s in pick]}


async def compute_iaa(db: AsyncSession, dataset_id: str, gold_set: m.GoldSet | None = None,
                      normalizer_kind: str = "arabic_v1") -> dict:
    """Agreement of each submitted double annotation against the approved primary text (corpus-level WER/CER)."""
    norm = plugins.normalizers.get(normalizer_kind)()
    wer, cer = plugins.metrics.get("wer")(), plugins.metrics.get("cer")()
    q = (select(m.Annotation).join(m.Segment, m.Segment.id == m.Annotation.segment_id)
         .join(m.AudioFile, m.AudioFile.id == m.Segment.audio_file_id)
         .where(m.AudioFile.dataset_id == dataset_id, m.Annotation.track == "iaa", m.Annotation.status == "submitted"))
    iaa = (await db.execute(q)).scalars().all()
    gold_ids = None
    if gold_set:
        gold_ids = set((await db.execute(select(m.GoldItem.segment_id).where(m.GoldItem.gold_set_id == gold_set.id))).scalars())
    per: dict[str, tuple[list, list]] = {}
    allw, allc = [], []
    for a in iaa:
        if gold_ids is not None and a.segment_id not in gold_ids:
            continue
        prim = (await db.execute(select(m.Annotation).where(
            m.Annotation.segment_id == a.segment_id, m.Annotation.track == "primary", m.Annotation.status == "approved")
            .order_by(m.Annotation.version.desc()).limit(1))).scalar_one_or_none()
        if prim is None:
            continue
        ref, hyp = norm.normalize(prim.text), norm.normalize(a.text)
        w, c = wer.score(ref, hyp), cer.score(ref, hyp)
        pw, pc = per.setdefault(a.author_id or "?", ([], []))
        pw.append(w), pc.append(c)
        allw.append(w), allc.append(c)

    def pack(ws, cs):
        W = wer.aggregate(ws)
        return {"pairs": len(ws), "wer": W, "cer": cer.aggregate(cs), "agreement": max(0.0, 1 - W)}

    return {**(pack(allw, allc) if allw else {"pairs": 0, "wer": None, "cer": None, "agreement": None}),
            "normalizer": {"kind": norm.kind, "version": norm.version},
            "per_annotator": [{"user_id": u, **pack(*v)} for u, v in sorted(per.items())]}
