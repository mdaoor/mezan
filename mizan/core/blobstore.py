"""BlobStore interface + local-filesystem implementation (spec 3.2 [OPEN]: local FS chosen, see DECISIONS.md)."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    def local_path(self, key: str) -> Path: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def put_file(self, key: str, src: Path, move: bool = True) -> None: ...
    def read(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> None: ...


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def local_path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("blob key escapes store root")
        return p

    def put_bytes(self, key: str, data: bytes) -> None:
        p = self.local_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def put_file(self, key: str, src: Path, move: bool = True) -> None:
        p = self.local_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        (shutil.move if move else shutil.copyfile)(str(src), str(p))

    def read(self, key: str) -> bytes:
        return self.local_path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self.local_path(key).exists()

    def delete_prefix(self, prefix: str) -> None:
        p = self.local_path(prefix)
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)

    async def aread(self, key: str) -> bytes:
        return await asyncio.to_thread(self.read, key)


def audio_prefix(pid: str, did: str, afid: str) -> str:
    return f"projects/{pid}/datasets/{did}/audio/{afid}"
