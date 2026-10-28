"""Cut segment audio on the fly from normalized.wav (no per-segment files)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.audio import WavInfo, cut_wav, parse_wav


class SegmentAudioReader:
    def __init__(self, c):
        self.c = c
        self._files: dict[str, tuple[Path, WavInfo]] = {}

    async def prepare(self, db: AsyncSession, segments: list[m.Segment]) -> None:
        ids = {s.audio_file_id for s in segments} - set(self._files)
        if not ids:
            return
        rows = (await db.execute(select(m.AudioFile).where(m.AudioFile.id.in_(ids)))).scalars().all()
        for af in rows:
            if not af.normalized_blob:
                continue
            p = self.c.blobs.local_path(af.normalized_blob)
            self._files[af.id] = (p, await asyncio.to_thread(parse_wav, p))

    async def wav(self, seg: m.Segment) -> bytes:
        path, info = self._files[seg.audio_file_id]
        return await asyncio.to_thread(cut_wav, path, info, seg.start_s, seg.end_s)
