"""Developer accounts + scoped API keys (spec §134).

Key format: ``fos_<mode>_<prefix>_<secret>`` (mode = test|live). Only an
HMAC-SHA256 (server SECRET_KEY as pepper) of the full key is stored; the
plaintext is returned exactly once at creation. Lookup is by the public
prefix, comparison is constant-time. Keys are revocable and may expire.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.core import Role, User, UserRole
from app.db.models.developer import Customer, CustomerApiKey
from app.db.models.enums import AuditEventType, RoleName

SCOPES = ("jobs:read", "jobs:write", "billing:read", "billing:write", "webhooks:write")
DEFAULT_SCOPES = ("jobs:read", "jobs:write", "billing:read")
KEY_PREFIX = "fos_"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def key_mode() -> str:
    from app.services.developer import stripe_client
    return "live" if stripe_client.mode() == "live" else "test"


def hash_key(full_key: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), full_key.encode(), hashlib.sha256).hexdigest()


def parse_key(full_key: str) -> Optional[tuple[str, str]]:
    """Return (mode, prefix) for a well-formed key, else None."""
    parts = full_key.split("_")
    if len(parts) != 4 or parts[0] != "fos" or parts[1] not in ("test", "live") or len(parts[2]) != 12:
        return None
    return parts[1], parts[2]


@dataclass
class Principal:
    customer: Customer
    user_id: Optional[uuid.UUID]
    via: str                              # "jwt" | "api_key"
    scopes: set[str] = field(default_factory=set)
    api_key_id: Optional[uuid.UUID] = None

    def has(self, scope: str) -> bool:
        return scope in self.scopes


async def customer_for_user(db: AsyncSession, user_id: uuid.UUID) -> Optional[Customer]:
    return (await db.execute(select(Customer).where(Customer.user_id == user_id))).scalar_one_or_none()


async def ensure_customer(db: AsyncSession, user: User, name: Optional[str] = None) -> tuple[Customer, bool]:
    existing = await customer_for_user(db, user.id)
    if existing is not None:
        return existing, False
    cust = Customer(id=uuid.uuid4(), user_id=user.id, name=(name or user.full_name or user.email)[:255],
                    status="ACTIVE")
    try:
        async with db.begin_nested():
            db.add(cust)
            await db.flush()
    except IntegrityError:  # concurrent signup of the same user
        return await customer_for_user(db, user.id), False
    role = (await db.execute(select(Role).where(Role.name == RoleName.DEVELOPER))).scalar_one_or_none()
    if role is not None:
        has = (await db.execute(select(UserRole).where(UserRole.user_id == user.id,
                                                       UserRole.role_id == role.id))).scalar_one_or_none()
        if has is None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
    db.add(AuditLog(event_type=AuditEventType.CUSTOMER_CREATED, actor_type="USER", actor_id=user.id,
                    target_type="CUSTOMER", target_id=cust.id, event_metadata={"name": cust.name}))
    return cust, True


def validate_scopes(scopes: Optional[list[str]]) -> list[str]:
    scopes = list(scopes or DEFAULT_SCOPES)
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scopes: {', '.join(bad)}; allowed: {', '.join(SCOPES)}")
    return sorted(set(scopes))


async def create_key(db: AsyncSession, customer: Customer, name: str, scopes: Optional[list[str]],
                     expires_in_days: Optional[int], actor_id: Optional[uuid.UUID]) -> tuple[CustomerApiKey, str]:
    clean = validate_scopes(scopes)
    prefix = secrets.token_hex(6)
    full = f"{KEY_PREFIX}{key_mode()}_{prefix}_{secrets.token_urlsafe(32).replace('_', '-')}"
    row = CustomerApiKey(id=uuid.uuid4(), customer_id=customer.id, name=name[:100], prefix=prefix,
                         secret_hash=hash_key(full), scopes=clean,
                         expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None)
    db.add(row)
    db.add(AuditLog(event_type=AuditEventType.API_KEY_CREATED, actor_type="USER", actor_id=actor_id,
                    target_type="CUSTOMER", target_id=customer.id,
                    event_metadata={"key_id": str(row.id), "prefix": prefix, "scopes": clean}))
    await db.flush()
    return row, full


async def revoke_key(db: AsyncSession, customer: Customer, key_id: uuid.UUID, actor_id: Optional[uuid.UUID]) -> CustomerApiKey:
    row = (await db.execute(select(CustomerApiKey).where(CustomerApiKey.id == key_id,
                                                         CustomerApiKey.customer_id == customer.id))).scalar_one_or_none()
    if row is None:
        raise KeyError(key_id)
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        db.add(AuditLog(event_type=AuditEventType.API_KEY_REVOKED, actor_type="USER", actor_id=actor_id,
                        target_type="CUSTOMER", target_id=customer.id,
                        event_metadata={"key_id": str(row.id), "prefix": row.prefix}))
    return row


async def authenticate_key(db: AsyncSession, full_key: str) -> Optional[Principal]:
    parsed = parse_key(full_key)
    if parsed is None:
        return None
    mode, prefix = parsed
    row = (await db.execute(select(CustomerApiKey).where(CustomerApiKey.prefix == prefix))).scalar_one_or_none()
    if row is None or not hmac.compare_digest(row.secret_hash, hash_key(full_key)):
        return None
    now = utcnow()
    if row.revoked_at is not None or (row.expires_at is not None and now >= row.expires_at):
        return None
    cust = (await db.execute(select(Customer).where(Customer.id == row.customer_id))).scalar_one_or_none()
    if cust is None or cust.status == "CLOSED":
        return None
    if row.last_used_at is None or (now - row.last_used_at).total_seconds() >= settings.API_KEY_TOUCH_SECONDS:
        row.last_used_at = now
        await db.commit()
    return Principal(customer=cust, user_id=None, via="api_key", scopes=set(row.scopes or []), api_key_id=row.id)


def key_dict(row: CustomerApiKey) -> dict:
    return {"key_id": str(row.id), "name": row.name, "prefix": row.prefix, "scopes": list(row.scopes or []),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "active": row.revoked_at is None and (row.expires_at is None or utcnow() < row.expires_at)}
