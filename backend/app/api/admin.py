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
                "reputation_score": float(d.reputation_score) if d.reputation_score is not None else None,
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
    # Probation: a device quarantined for low reputation restarts just above the floor.
    from app.config import settings as _settings
    floor = float(_settings.REPUTATION_QUARANTINE_BELOW) + 5.0
    if float(device.reputation_score or 0) < floor:
        device.reputation_score = floor
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


# ── Phase 2: leases, dead-letter, canaries ───────────────────────────────────

class CanaryRequest(BaseModel):
    device_id: str
    workload_type: str = "checksum"
    url: str | None = None
    expected_http_status: int | None = None


@router.post("/jobs/sweep-leases")
async def admin_sweep_leases(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    """Expire overdue leases now: requeue (or dead-letter) their jobs."""
    from app.services import lease_service as leases

    swept = await leases.sweep_expired(db, limit=500)
    return {"swept": swept, "count": len(swept)}


@router.get("/jobs/dead-letter")
async def admin_dead_letter(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    rows = (await db.execute(
        select(Job).where(Job.dead_lettered.is_(True)).order_by(Job.updated_at.desc()).limit(200)
    )).scalars().all()
    return {"jobs": [{"job_id": str(j.id), "workload_type": j.workload_type, "status": j.status.value,
                      "retry_count": j.retry_count, "max_retries": j.max_retries, "last_error": j.last_error,
                      "parent_job_id": str(j.parent_job_id) if j.parent_job_id else None,
                      "updated_at": j.updated_at.isoformat()} for j in rows],
            "total": len(rows)}


@router.post("/jobs/{job_id}/requeue")
async def admin_requeue_dead_letter(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN")),
):
    """Explicitly move a dead-lettered job back to QUEUED with a fresh retry budget."""
    from app.db.models.enums import JobEventActorType
    from app.services import lease_service as leases

    job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.dead_lettered:
        raise HTTPException(status_code=409, detail="Job is not dead-lettered")
    await leases.clear_attempt(db, job)
    leases.add_event(db, job.id, job.status.value, JobStatus.QUEUED.value, reason="admin requeue from dead-letter",
                     actor_type=JobEventActorType.ADMIN, actor_id=current_user.id)
    job.status = JobStatus.QUEUED
    job.dead_lettered = False
    job.retry_count = 0
    job.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"job_id": str(job.id), "status": job.status.value}


@router.get("/jobs/{job_id}/attempts")
async def admin_job_attempts(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    """Lease/attempt history plus the immutable job_event trail."""
    from app.db.models.jobs_ledger import JobEvent, JobLease
    from app.services import lease_service as leases

    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    lease_rows = (await db.execute(
        select(JobLease).where(JobLease.job_id == job.id).order_by(JobLease.attempt, JobLease.issued_at)
    )).scalars().all()
    events = (await db.execute(
        select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id)
    )).scalars().all()
    return {
        "job_id": str(job.id), "status": job.status.value, "retry_count": job.retry_count,
        "max_retries": job.max_retries, "dead_lettered": job.dead_lettered, "last_error": job.last_error,
        "role": (job.verification_spec or {}).get("role", "PRIMARY"),
        "attempts": [{**leases.lease_dict(l), "device_id": str(l.device_id), "result_hash": l.result_hash,
                      "closed_at": l.closed_at.isoformat() if l.closed_at else None,
                      "verification": (l.evidence or {}).get("verification")} for l in lease_rows],
        "events": [{"from": e.from_status, "to": e.to_status, "reason": e.reason,
                    "actor_type": e.actor_type.value if hasattr(e.actor_type, "value") else e.actor_type,
                    "at": e.created_at.isoformat() if e.created_at else None} for e in events],
    }


@router.post("/jobs/canary")
async def admin_create_canary(
    req: CanaryRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    """Dispatch a known-answer spot-check job to a device (spec §67 KNOWN_ANSWER).

    The expected answer is stored server-side only; the job looks like any other
    job of that type to the device. Canary jobs never earn credit; a wrong answer
    applies the canary reputation penalty and quarantines the device.
    """
    import secrets
    from app.db.models.enums import JobEventActorType
    from app.db.models.jobs_ledger import JobAssignment
    from app.services import benchmark_service as bench_svc
    from app.services import lease_service as leases
    from app.services import verification_service as vsvc
    from app.services.device_safety import dispatch_block_reason

    device = (await db.execute(select(Device).where(Device.id == req.device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    try:
        payload, expected = vsvc.canary_payload_and_expected(
            req.workload_type, secrets.token_hex(8), req.url, req.expected_http_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = datetime.now(timezone.utc)
    blocked = dispatch_block_reason(device, req.workload_type, await bench_svc.thresholds_for_device(db, device), now)
    if blocked:
        raise HTTPException(status_code=409, detail=f"Dispatch blocked by safety policy: {blocked}")
    job = Job(workload_type=req.workload_type, status=JobStatus.DISPATCHED, priority=100,
              requested_resources=payload, adapter_id="internal_compute", customer_id=current_user.id,
              max_retries=0, retry_count=0,
              verification_spec={"role": vsvc.CANARY, "expected": expected})
    db.add(job)
    await db.flush()
    leases.add_event(db, job.id, None, JobStatus.CREATED.value, reason="canary spot-check",
                     actor_type=JobEventActorType.ADMIN, actor_id=current_user.id)
    leases.add_event(db, job.id, JobStatus.CREATED.value, JobStatus.DISPATCHED.value,
                     reason=f"canary to {device.human_id}", actor_type=JobEventActorType.ADMIN, actor_id=current_user.id)
    db.add(JobAssignment(job_id=job.id, device_id=device.id, assigned_at=now))
    lease = await leases.issue_lease(db, job, device.id, now)
    await db.commit()
    return {"job_id": str(job.id), "device_id": str(device.id), "workload_type": job.workload_type,
            "status": job.status.value, "lease": leases.lease_dict(lease)}
