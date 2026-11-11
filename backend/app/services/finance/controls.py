"""Finance kill switches (spec §124, §139) and platform risk events (spec §137)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.core import SystemSettings
from app.db.models.enums import AuditEventType
from app.db.models.finance import RiskEvent

KILL_KEY = "finance_kill_switches"
SWITCHES = ("PAUSE_WITHDRAWALS", "PAUSE_PAYOUTS", "PAUSE_REVENUE_CONFIRMATION")


class FinanceError(Exception):
    """Business-rule refusal; API maps it to ``status_code``."""

    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def kill_switches(db: AsyncSession) -> dict[str, Any]:
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == KILL_KEY))).scalar_one_or_none()
    value = dict(row.value or {}) if row else {}
    return {name: dict(value.get(name) or {"enabled": False}) for name in SWITCHES}


async def is_paused(db: AsyncSession, name: str) -> bool:
    return bool((await kill_switches(db))[name].get("enabled"))


async def set_switch(db: AsyncSession, name: str, enabled: bool, reason: str | None,
                     actor_id: uuid.UUID | None, actor_type: str = "ADMIN") -> dict[str, Any]:
    if name not in SWITCHES:
        raise ValueError(f"unknown kill switch {name}")
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == KILL_KEY)
                            .with_for_update())).scalar_one_or_none()
    value = dict(row.value or {}) if row else {}
    value[name] = {"enabled": enabled, "reason": reason, "by": str(actor_id) if actor_id else actor_type,
                   "at": utcnow().isoformat()}
    if row is None:
        db.add(SystemSettings(key=KILL_KEY, value=value, description="Finance kill switches", updated_by=actor_id))
    else:
        row.value = value
        row.updated_by = actor_id
    db.add(AuditLog(event_type=AuditEventType.KILL_SWITCH_CHANGED, actor_type=actor_type, actor_id=actor_id,
                    target_type="KILL_SWITCH", event_metadata={"switch": name, "enabled": enabled, "reason": reason}))
    await db.flush()
    return value[name]


async def raise_risk(db: AsyncSession, category: str, message: str, *, severity: str = "MEDIUM",
                     subject_type: str | None = None, subject_id: uuid.UUID | None = None,
                     details: dict | None = None) -> RiskEvent:
    """Open a risk event (deduplicated while an identical one is still OPEN)."""
    existing = (await db.execute(select(RiskEvent).where(
        RiskEvent.category == category, RiskEvent.message == message, RiskEvent.status == "OPEN",
        RiskEvent.subject_type.is_(subject_type) if subject_type is None else RiskEvent.subject_type == subject_type,
        RiskEvent.subject_id.is_(None) if subject_id is None else RiskEvent.subject_id == subject_id,
    ))).scalars().first()
    if existing is not None:
        existing.details = {**(existing.details or {}), **(details or {}), "last_seen": utcnow().isoformat()}
        return existing
    ev = RiskEvent(category=category, severity=severity, subject_type=subject_type, subject_id=subject_id,
                   message=message[:2000], details=details or {}, status="OPEN")
    db.add(ev)
    await db.flush()
    return ev


async def has_open_risk(db: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> bool:
    return (await db.execute(select(RiskEvent.id).where(
        RiskEvent.subject_type == subject_type, RiskEvent.subject_id == subject_id,
        RiskEvent.status == "OPEN").limit(1))).first() is not None


async def resolve_risk(db: AsyncSession, event_id: uuid.UUID, resolution: str, actor_id: uuid.UUID | None) -> Optional[RiskEvent]:
    ev = (await db.execute(select(RiskEvent).where(RiskEvent.id == event_id))).scalar_one_or_none()
    if ev is None:
        return None
    ev.status, ev.resolved_at, ev.resolved_by, ev.resolution = "RESOLVED", utcnow(), actor_id, resolution
    db.add(AuditLog(event_type=AuditEventType.RISK_EVENT_RESOLVED, actor_type="ADMIN", actor_id=actor_id,
                    target_type="RISK_EVENT", target_id=ev.id, event_metadata={"resolution": resolution}))
    return ev


def risk_dict(ev: RiskEvent) -> dict[str, Any]:
    return {"risk_event_id": str(ev.id), "category": ev.category, "severity": ev.severity,
            "subject_type": ev.subject_type, "subject_id": str(ev.subject_id) if ev.subject_id else None,
            "message": ev.message, "details": ev.details or {}, "status": ev.status,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
            "resolved_at": ev.resolved_at.isoformat() if ev.resolved_at else None, "resolution": ev.resolution}
