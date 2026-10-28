from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from mizan.core import models as m
from mizan.core.errors import NotFound

T = TypeVar("T")


@dataclass
class Actor:
    id: str
    role: str
    name: str = ""

    def dict(self) -> dict:
        return {"id": self.id, "role": self.role, "name": self.name}


async def must_get(db: AsyncSession, model: type[T], id_: str, what: str = "") -> T:
    obj = await db.get(model, id_)
    if obj is None:
        raise NotFound(f"{what or model.__name__} {id_} not found")
    return obj


def audit(db: AsyncSession, actor: Actor | None, action: str, entity: str, entity_id: str | None, diff: dict | None = None):
    db.add(m.AuditLog(actor_id=actor.id if actor else None, action=action, entity=entity,
                      entity_id=entity_id, diff=diff or {}))


def page_params(limit: int | None, cursor: str | None, default: int = 50, maximum: int = 500) -> tuple[int, int]:
    """Opaque offset cursor (stringified int)."""
    lim = max(1, min(limit or default, maximum))
    try:
        off = int(cursor) if cursor else 0
    except ValueError:
        off = 0
    return lim, max(0, off)


def paginate(items: list, limit: int, offset: int) -> dict:
    nxt = str(offset + limit) if len(items) > limit else None
    return {"items": items[:limit], "next_cursor": nxt}
