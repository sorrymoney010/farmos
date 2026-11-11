"""Idempotency-Key support for customer write endpoints (spec §136)."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.developer import IdempotencyRecord


class IdempotencyConflict(Exception):
    pass


def request_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


async def lookup(db: AsyncSession, scope: str, key: Optional[str], body: Any) -> Optional[tuple[int, Any]]:
    if not key:
        return None
    rec = (await db.execute(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope,
                                                            IdempotencyRecord.key == key))).scalar_one_or_none()
    if rec is None:
        return None
    if rec.request_hash != request_hash(body):
        raise IdempotencyConflict("Idempotency-Key was already used with a different request body")
    return rec.status_code, rec.response


def remember(db: AsyncSession, scope: str, key: Optional[str], body: Any, status_code: int, response: Any) -> None:
    if key:
        db.add(IdempotencyRecord(id=uuid.uuid4(), scope=scope[:100], key=key[:128], request_hash=request_hash(body),
                                 status_code=status_code, response=response))
