"""Device reputation + verification-failure quarantine (spec §36, §37, §67).

Every verification outcome and lease timeout feeds the device's
``reputation_score`` (0..100, default 50). Repeated bad outputs within a
window, a failed canary (known-answer) check, or reputation falling below the
floor auto-quarantines the device. QUARANTINED -> IDLE only via explicit admin
release (spec §60).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.devices import Device, DeviceHealthEvent
from app.db.models.enums import DeviceStatus
from app.logging import get_logger

logger = get_logger(__name__)

PASS = "PASS"
FAIL = "FAIL"            # result failed verification (wrong / forged output)
SOFT_FAIL = "SOFT_FAIL"  # device reported FAILED, or inconclusive redundant disagreement
TIMEOUT = "TIMEOUT"      # lease expired
CANARY_FAIL = "CANARY_FAIL"

FAILURE_EVENT_TYPES = ("VERIFICATION_FAILED", "CANARY_FAILED")

_EVENT_FOR_KIND = {
    FAIL: ("VERIFICATION_FAILED", "WARNING"),
    CANARY_FAIL: ("CANARY_FAILED", "CRITICAL"),
    SOFT_FAIL: ("JOB_FAILED", "INFO"),
    TIMEOUT: ("LEASE_EXPIRED", "WARNING"),
}


def delta_for(kind: str) -> float:
    return {
        PASS: settings.REPUTATION_PASS_DELTA,
        FAIL: settings.REPUTATION_FAIL_DELTA,
        SOFT_FAIL: settings.REPUTATION_TIMEOUT_DELTA,
        TIMEOUT: settings.REPUTATION_TIMEOUT_DELTA,
        CANARY_FAIL: settings.REPUTATION_CANARY_FAIL_DELTA,
    }[kind]


def clamp(score: float) -> float:
    return max(0.0, min(100.0, score))


async def _recent_failures(db: AsyncSession, device_id: UUID, now: datetime) -> int:
    since = now - timedelta(seconds=settings.VERIFICATION_FAILURE_WINDOW_SECONDS)
    last_release = (
        await db.execute(
            select(func.max(DeviceHealthEvent.created_at)).where(
                DeviceHealthEvent.device_id == device_id,
                DeviceHealthEvent.event_type == "RELEASE_QUARANTINE",
            )
        )
    ).scalar()
    if last_release is not None and last_release > since:
        since = last_release
    return int(
        (
            await db.execute(
                select(func.count(DeviceHealthEvent.id)).where(
                    DeviceHealthEvent.device_id == device_id,
                    DeviceHealthEvent.event_type.in_(FAILURE_EVENT_TYPES),
                    DeviceHealthEvent.created_at >= since,
                )
            )
        ).scalar()
        or 0
    )


async def record_outcome(
    db: AsyncSession,
    device: Device,
    kind: str,
    *,
    job_id: UUID | None = None,
    detail: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a reputation delta and (maybe) quarantine. Caller commits."""
    now = now or datetime.now(timezone.utc)
    before = float(device.reputation_score or 0)
    after = clamp(before + delta_for(kind))
    device.reputation_score = after
    quarantined = False
    reason = None

    if kind in _EVENT_FOR_KIND:
        etype, severity = _EVENT_FOR_KIND[kind]
        db.add(DeviceHealthEvent(
            device_id=device.id, event_type=etype, severity=severity,
            message=(detail or etype)[:2000],
            event_metadata={"job_id": str(job_id) if job_id else None,
                            "reputation_before": before, "reputation_after": after},
        ))
        await db.flush()

    if kind in (FAIL, CANARY_FAIL, TIMEOUT) and device.status != DeviceStatus.QUARANTINED:
        failures = await _recent_failures(db, device.id, now) if kind != TIMEOUT else 0
        if kind == CANARY_FAIL:
            reason = "failed known-answer canary check"
        elif failures >= settings.VERIFICATION_FAILURES_QUARANTINE:
            reason = f"{failures} verification failures within window"
        elif after < settings.REPUTATION_QUARANTINE_BELOW:
            reason = f"reputation {after:.1f} below {settings.REPUTATION_QUARANTINE_BELOW:.1f}"
        if reason:
            quarantined = True
            device.status = DeviceStatus.QUARANTINED
            device.safety_paused = True
            device.safety_reason = f"verification: {reason}"
            db.add(DeviceHealthEvent(
                device_id=device.id, event_type="QUARANTINE", severity="CRITICAL",
                message=f"Auto-quarantine: {reason}",
                event_metadata={"actor": "VERIFICATION_POLICY", "job_id": str(job_id) if job_id else None},
            ))
            logger.warning("device_auto_quarantined", device_id=str(device.id), reason=reason)

    return {"kind": kind, "reputation_before": before, "reputation_after": after,
            "quarantined": quarantined, "quarantine_reason": reason}
