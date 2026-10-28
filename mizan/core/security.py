from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

ROLES = ("viewer", "annotator", "reviewer", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(rounds=10)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def make_token(secret: str, user_id: str, role: str, ttl_h: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=ttl_h)
    return jwt.encode({"sub": user_id, "role": role, "exp": exp}, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


def role_at_least(role: str, minimum: str) -> bool:
    return _RANK.get(role, -1) >= _RANK[minimum]
