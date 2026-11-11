"""Admin endpoints for FARMOS."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.core import User, Provider
from app.db.models.devices import Device
from app.db.models.devices import DeviceHeartbeat, DeviceHealthEvent
from app.db.models.jobs_ledger import Job, LedgerEntry, Withdrawal
from app.db.models.audit import AuditLog
from app.db.models.enums import (
    DeviceStatus,
    JobStatus,
    WithdrawalStatus,
    RoleName,
    AuditEventType,
)
from app.db.session import get_db
from app.dependencies import require_roles, get_current_user
from app.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


class AdminDeviceActionRequest(BaseModel):
    device_id: str
    reason: str | None = None


@router.get("/fleet")
async def admin_fleet(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    devices_result = await db.execute(select(Device).limit(100))
    devices = devices_result.scalars().all()
    return {
        "devices": [
            {
                "device_id": str(d.id),
                "human_id": d.human_id,
                "status": d.status.value,
                "farm_score": float(d.farm_score) if d.farm_score is not None else None,
                "temperature_state": d.temperature_state or "UNKNOWN",
                "temperature_c": float(d.last_temperature_c) if d.last_temperature_c is not None else None,
                "battery_pct": float(d.last_battery_pct) if d.last_battery_pct is not None else None,
                "charging": d.last_charging,
                "safety_paused": bool(d.safety_paused),
                "safety_reason": d.safety_reason,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            }
            for d in devices
        ],
        "total": len(devices),
    }


@router.get("/users")
async def admin_users(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN")),
):
    result = await db.execute(select(User).limit(100))
    users = result.scalars().all()
    return [
        {
            "user_id": str(u.id),
            "email": u.email,
            "status": u.status.value,
            "is_superuser": u.is_superuser,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("/devices/{device_id}/quarantine")
async def quarantine_device(
    device_id: str,
    req: AdminDeviceActionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    device.status = DeviceStatus.QUARANTINED
    event = DeviceHealthEvent(
        device_id=device.id,
        event_type="QUARANTINE",
        severity="WARNING",
        message=req.reason if req else "Admin quarantine",
        event_metadata={"actor_id": str(current_user.id)},
    )
    db.add(event)
    await db.commit()
    logger.info("device_quarantined", device_id=str(device.id), by=str(current_user.id))
    return {"detail": "Device quarantined", "device_id": device_id}


@router.post("/devices/{device_id}/release")
async def release_quarantine(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN")),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.status != DeviceStatus.QUARANTINED:
        raise HTTPException(status_code=409, detail=f"Device is not quarantined (status={device.status.value})")
    # Explicit admin release (spec §60: QUARANTINED -> ACTIVE only via explicit release).
    # Safety state is reset; the next heartbeat re-evaluates thermal/battery policy.
    device.status = DeviceStatus.IDLE
    device.safety_paused = False
    device.safety_reason = None
    device.cooldown_until = None
    event = DeviceHealthEvent(
        device_id=device.id,
        event_type="RELEASE_QUARANTINE",
        severity="INFO",
        message="Released from quarantine",
        event_metadata={"actor_id": str(current_user.id)},
    )
    db.add(event)
    await db.commit()
    return {"detail": "Device released", "device_id": device_id}


@router.get("/revenue")
async def admin_revenue(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "FINANCE")),
):
    # Aggregated ledger summary
    credit_result = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(LedgerEntry.direction == "CREDIT")
    )
    debit_result = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(LedgerEntry.direction == "DEBIT")
    )
    total_credits = float(credit_result.scalar() or 0)
    total_debits = float(debit_result.scalar() or 0)
    return {
        "total_ledger_credits": total_credits,
        "total_ledger_debits": total_debits,
        "net_position": total_credits - total_debits,
    }


@router.get("/risk-events")
async def admin_risk(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    events_result = await db.execute(
        select(DeviceHealthEvent).order_by(DeviceHealthEvent.created_at.desc()).limit(50)
    )
    events = events_result.scalars().all()
    return [
        {
            "event_id": str(e.id),
            "device_id": str(e.device_id),
            "event_type": e.event_type,
            "severity": e.severity,
            "message": e.message,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


@router.get("/health")
async def admin_health():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Sprint 3: FarmScore weights + thermal/battery safety policy ─────────────

class FarmScoreWeightsRequest(BaseModel):
    weights: dict[str, float]
    recalculate: bool = True


@router.get("/farmscore/weights")
async def get_farmscore_weights(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    from app.services import benchmark_service as bench_svc
    from app.services.farm_score import COMPONENTS, SUITE_VERSION

    return {"weights": await bench_svc.get_weights(db), "components": list(COMPONENTS),
            "suite_version": SUITE_VERSION}


@router.put("/farmscore/weights")
async def put_farmscore_weights(
    req: FarmScoreWeightsRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN")),
):
    from app.services import benchmark_service as bench_svc

    try:
        weights = await bench_svc.set_weights(db, req.weights, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.flush()
    recalculated = await bench_svc.recalculate_all_farm_scores(db) if req.recalculate else 0
    db.add(AuditLog(
        event_type=AuditEventType.SETTINGS_CHANGED, actor_type="USER", actor_id=current_user.id,
        target_type="system_settings", event_metadata={"key": bench_svc.WEIGHTS_KEY, "weights": weights},
    ))
    await db.commit()
    return {"weights": weights, "devices_recalculated": recalculated}


class SafetyPolicyRequest(BaseModel):
    default: dict[str, float] = {}
    models: dict[str, dict[str, float]] = {}


@router.get("/safety/policy")
async def get_safety_policy(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    from app.config import settings
    from app.services import benchmark_service as bench_svc
    from app.services.device_safety import thresholds_from_settings

    policy = await bench_svc.get_safety_policy(db)
    return {"policy": policy or {}, "effective_default": thresholds_from_settings(settings, None, policy).as_dict()}


@router.put("/safety/policy")
async def put_safety_policy(
    req: SafetyPolicyRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN")),
):
    from app.services import benchmark_service as bench_svc

    policy = req.model_dump()
    try:
        effective = await bench_svc.set_safety_policy(db, policy, current_user.id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid safety policy: {exc}")
    db.add(AuditLog(
        event_type=AuditEventType.SETTINGS_CHANGED, actor_type="USER", actor_id=current_user.id,
        target_type="system_settings", event_metadata={"key": bench_svc.SAFETY_POLICY_KEY, "policy": policy},
    ))
    await db.commit()
    return {"policy": policy, "effective_default": effective.as_dict()}
