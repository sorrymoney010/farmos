"""FARMOS job endpoints — dispatch, leases, device accept/renew, result submission,
verification, device polling (spec §59, §60, §67, §69-§71, §136)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models.enums import (
    DeviceStatus,
    JobEventActorType,
    JobStatus,
    JobType,
    LedgerAccountType,
    LedgerDirection,
)
from app.db.models.core import Role, UserRole
from app.db.models.devices import Device
from app.db.models.jobs_ledger import (
    Job,
    JobAssignment,
    JobResult,
    LedgerAccount,
    LedgerEntry,
)
from app.db.session import get_db
from app.dependencies import require_roles
from app.logging import get_logger
from app.security.node_auth import (
    ensure_timestamp_fresh,
    mark_nonce_used,
    verify_node_token,
    verify_request_signature,
)
from app.services import benchmark_service as bench_svc
from app.services import economics_service as econ
from app.services import lease_service as leases
from app.services import opportunity_service as opps
from app.services import profitability as prof
from app.services import verification_service as vsvc
from app.services.device_safety import SCHEDULABLE_STATUSES, dispatch_block_reason
from app.workloads import cpu_data

logger = get_logger(__name__)
router = APIRouter()

SYSTEM_WORKLOADS = {"benchmark"}


# ── Shared request models ─────────────────────────────────────────────────────

class JobCreateRequest(BaseModel):
    workload_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_revenue_usd: float | None = None
    expected_cost_usd: float | None = None
    max_runtime_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    priority: int = 100
    device_id: UUID | None = None  # optional: device affinity (scheduler only picks this device)
    opportunity_id: UUID | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)  # device_affinity, min_farm_score, ram_mb, tier
    deadline_at: datetime | None = None
    auto_schedule: bool | None = None  # default JOB_AUTO_SCHEDULE_DEFAULT


class JobDispatchRequest(BaseModel):
    job_id: UUID
    device_id: UUID


class JobAcceptRequest(BaseModel):
    request_nonce: str
    request_timestamp: str
    signature: str


class LeaseRenewRequest(BaseModel):
    request_nonce: str
    request_timestamp: str
    lease_id: UUID
    signature: str


class JobResultSubmitRequest(BaseModel):
    request_nonce: str
    request_timestamp: str
    signature: str
    result_hash: str = Field(min_length=64, max_length=128)
    result_uri: str | None = Field(default=None, max_length=2_000_000)
    execution_metrics: dict[str, Any] = Field(default_factory=dict)


def _payload_of(job: Job) -> dict[str, Any]:
    if not job.requested_resources:
        return {}
    try:
        return json.loads(job.requested_resources) if isinstance(job.requested_resources, str) else dict(job.requested_resources)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"raw": str(job.requested_resources)}


def _job_summary(job: Job) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "workload_type": job.workload_type,
        "priority": job.priority,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "dead_lettered": job.dead_lettered,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def _verification_view(job: Job) -> dict[str, Any] | None:
    v = job.verification
    if v is None:
        return None
    ev = v.evidence or {}
    return {
        "status": v.status,
        "method": v.method,
        "confidence": ev.get("confidence"),
        "reason": ev.get("reason"),
        "verified_at": v.verified_at.isoformat() if v.verified_at else None,
    }


# ── Create job ────────────────────────────────────────────────────────────────

@router.post("/")
async def create_job(
    req: JobCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    job_type = req.workload_type
    try:
        JobType(job_type)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported workload_type: {job_type}")
    if job_type in cpu_data.CPU_DATA_WORKLOADS:
        try:
            cpu_data.validate_payload(job_type, req.payload)
        except cpu_data.WorkloadError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid payload: {exc}")

    # Spec §136: duplicate Idempotency-Key returns the original job.
    if idempotency_key:
        scoped_key = f"{current_user.id}:{idempotency_key}"
        existing = (await db.execute(select(Job).where(Job.idempotency_key == scoped_key))).scalar_one_or_none()
        if existing is not None:
            return {**_job_summary(existing), "idempotent_replay": True}
    else:
        scoped_key = None

    # ── Opportunity + adapter kill switch (spec §13/§14) ──
    opp = None
    if req.opportunity_id is not None:
        opp = await opps.get_opportunity(db, req.opportunity_id)
        if opp is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
        if opp.workload_type and opp.workload_type != job_type:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Opportunity runs {opp.workload_type}, not {job_type}")
    elif job_type not in SYSTEM_WORKLOADS:
        opp = await opps.internal_opportunity(db, job_type)
    adapter_id = opp.adapter_id if opp is not None else opps.INTERNAL_ADAPTER_ID
    if not await opps.adapter_enabled(db, adapter_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Adapter {adapter_id} is disabled (kill switch)")
    blocked = opps.opportunity_block_reason(opp, datetime.now(timezone.utc))
    if blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Opportunity unavailable: {blocked}")

    constraints = {k: v for k, v in (req.constraints or {}).items()
                   if k in {"device_affinity", "min_farm_score", "ram_mb", "cpu_pct", "bandwidth_mbps", "tier"}}
    if req.device_id is not None:
        constraints["device_affinity"] = [str(req.device_id)]
    if req.expected_revenue_usd is not None:
        constraints["customer_price_usd"] = float(req.expected_revenue_usd)
    if "tier" in constraints and str(constraints["tier"]).upper() not in prof.TIER_MULTIPLIER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tier must be STANDARD|PRIORITY|ENTERPRISE")

    job = Job(
        workload_type=job_type,
        status=JobStatus.CREATED,
        priority=req.priority,
        requested_resources=req.payload,
        expected_revenue_usd=req.expected_revenue_usd,
        expected_cost_usd=req.expected_cost_usd,
        max_runtime_seconds=req.max_runtime_seconds,
        max_retries=req.max_retries if req.max_retries is not None else settings.JOB_DEFAULT_MAX_RETRIES,
        retry_count=0,
        idempotency_key=scoped_key,
        verification_spec={"role": vsvc.PRIMARY},
        constraints=constraints,
        deadline_at=req.deadline_at,
        opportunity_id=opp.id if opp is not None else None,
        adapter_id=adapter_id,
        customer_id=current_user.id,
    )
    db.add(job)
    await db.flush()
    leases.add_event(db, job.id, None, JobStatus.CREATED.value, actor_type=JobEventActorType.USER,
                     actor_id=current_user.id)

    # ── Profitability gate (spec §17/§64): reject work that cannot make money ──
    rejected_reason = None
    if job_type not in SYSTEM_WORKLOADS:
        terms = opps.terms_from(opp, job_type, constraints)
        est = prof.estimate_job(terms, prof.DeviceProfile(), await opps.cost_policy(db),
                                gross_override=constraints.get("customer_price_usd"),
                                job_max_runtime=req.max_runtime_seconds)
        score = prof.score_opportunity(est, terms, prof.DeviceProfile())
        await econ.record_estimate(db, job, None, est, status="ESTIMATED" if est.profitable else "REJECTED",
                                   opportunity_score=score, inputs={"reference_device": True})
        if not est.profitable:
            rejected_reason = (f"unprofitable: expected net ${est.expected_net_profit_usd:.8f} "
                               f"(gross ${est.adjusted_gross_usd:.8f} - costs ${est.total_cost_usd:.8f})")
    if rejected_reason:
        leases.add_event(db, job.id, JobStatus.CREATED.value, JobStatus.REJECTED.value, reason=rejected_reason)
        job.status = JobStatus.REJECTED
        job.last_error = rejected_reason
    else:
        leases.add_event(db, job.id, JobStatus.CREATED.value, JobStatus.VALIDATED.value, reason="payload, adapter and profitability ok")
        auto = settings.JOB_AUTO_SCHEDULE_DEFAULT if req.auto_schedule is None else req.auto_schedule
        if auto:
            leases.add_event(db, job.id, JobStatus.VALIDATED.value, JobStatus.QUEUED.value, reason="queued for scheduler")
            job.status = JobStatus.QUEUED
        else:
            job.status = JobStatus.VALIDATED
    await db.commit()
    return {**_job_summary(job), "idempotent_replay": False, "adapter_id": job.adapter_id,
            "opportunity_id": str(job.opportunity_id) if job.opportunity_id else None,
            "rejected_reason": rejected_reason,
            "expected_net_usd": float(job.expected_profit_usd) if job.expected_profit_usd is not None else None}


def visible_jobs(user_id: UUID):
    """Scope before pagination/lookup; customer ownership uses the user UUID.

    Deployment.customer_id is a different (billing account) identifier.
    Explicit staff roles retain operational access even on multi-role accounts.
    """
    staff = (
        select(UserRole.id)
        .join(Role, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.name.in_(("OWNER", "ADMIN", "OPERATIONS")))
        .exists()
    )
    return select(Job).where(or_(Job.customer_id == user_id, staff))


# ── List jobs ─────────────────────────────────────────────────────────────────

@router.get("/")
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS", "CUSTOMER")),
):
    result = await db.execute(visible_jobs(current_user.id).order_by(desc(Job.created_at)).limit(100))
    jobs = result.scalars().all()
    return [
        {
            **_job_summary(j),
            "expected_revenue_usd": float(j.expected_revenue_usd) if j.expected_revenue_usd else None,
            "expected_cost_usd": float(j.expected_cost_usd) if j.expected_cost_usd else None,
        }
        for j in jobs
    ]


# ── Get job ───────────────────────────────────────────────────────────────────

@router.get("/{job_id}")
async def get_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS", "CUSTOMER")),
):
    # Eager-load relationships: lazy loading is not allowed under asyncio.
    result = await db.execute(
        visible_jobs(current_user.id)
        .options(selectinload(Job.assignment), selectinload(Job.verification))
        .where(Job.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    lease = await leases.latest_lease(db, job.id)
    return {
        **_job_summary(job),
        "payload": _payload_of(job),
        "expected_revenue_usd": float(job.expected_revenue_usd) if job.expected_revenue_usd else None,
        "expected_cost_usd": float(job.expected_cost_usd) if job.expected_cost_usd else None,
        "max_runtime_seconds": job.max_runtime_seconds,
        "updated_at": job.updated_at.isoformat(),
        "last_error": job.last_error,
        "parent_job_id": str(job.parent_job_id) if job.parent_job_id else None,
        "role": vsvc.role_of(job),
        "lease": leases.lease_dict(lease),
        "verification": _verification_view(job),
        "assignment": (
            {
                "device_id": str(job.assignment.device_id),
                "assigned_at": job.assignment.assigned_at.isoformat(),
                "accepted_at": job.assignment.accepted_at.isoformat() if job.assignment.accepted_at else None,
            }
            if job.assignment
            else None
        ),
    }


# ── Dispatch job to device ────────────────────────────────────────────────────

@router.post("/dispatch")
async def dispatch_job(
    req: JobDispatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    job = (await db.execute(select(Job).where(Job.id == req.job_id).with_for_update())).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status not in {JobStatus.CREATED, JobStatus.VALIDATED, JobStatus.QUEUED}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job cannot be dispatched in status {job.status.value}",
        )
    if vsvc.role_of(job) == vsvc.PRIMARY and not await opps.adapter_enabled(db, job.adapter_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Adapter {job.adapter_id} is disabled (kill switch)")

    device = (await db.execute(select(Device).where(Device.id == req.device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    # Coder rule §53.9: no device job without policy check (status + thermal + battery).
    now = datetime.now(timezone.utc)
    thresholds = await bench_svc.thresholds_for_device(db, device)
    blocked = dispatch_block_reason(device, job.workload_type, thresholds, now)
    if blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Dispatch blocked by safety policy: {blocked}")

    # Record the device-specific estimate (manual dispatch is an admin override,
    # so it is not blocked on profitability, but the economics are tracked).
    if job.workload_type not in SYSTEM_WORKLOADS:
        opp = await opps.get_opportunity(db, job.opportunity_id) if job.opportunity_id else None
        terms = opps.terms_from(opp, job.workload_type, job.constraints)
        hb = (await econ.latest_heartbeats(db, [device.id])).get(device.id)
        de = (await econ.device_economics_rows(db, [device.id])).get(device.id)
        profile = econ.device_profile(device, hb, de)
        est = prof.estimate_job(terms, profile, await opps.cost_policy(db),
                                gross_override=(job.constraints or {}).get("customer_price_usd"),
                                job_max_runtime=job.max_runtime_seconds, retry_count=int(job.retry_count or 0))
        await econ.record_estimate(db, job, device.id, est, opportunity_score=prof.score_opportunity(est, terms, profile),
                                   fit=prof.device_fit(profile, prof.requirements_for(terms, job.constraints)),
                                   inputs={"manual_dispatch": True})

    assignment = JobAssignment(job_id=job.id, device_id=req.device_id, assigned_at=now)
    db.add(assignment)
    lease = await leases.issue_lease(db, job, device.id, now)
    leases.add_event(db, job.id, job.status.value, JobStatus.DISPATCHED.value,
                     reason=f"lease {lease.id} attempt {lease.attempt} to {device.human_id}",
                     actor_type=JobEventActorType.ADMIN, actor_id=current_user.id)
    job.status = JobStatus.DISPATCHED
    job.updated_at = now
    await db.commit()

    logger.info("job_dispatched", job_id=str(job.id), device_id=str(device.id),
                device_human_id=device.human_id, lease_id=str(lease.id))
    return {
        "job_id": str(job.id),
        "device_id": str(device.id),
        "device_human_id": device.human_id,
        "status": JobStatus.DISPATCHED.value,
        "assigned_at": assignment.assigned_at.isoformat(),
        "lease": leases.lease_dict(lease),
    }


# ── Device auth helpers ───────────────────────────────────────────────────────

node_bearer = HTTPBearer(auto_error=False)


async def _node_device(db: AsyncSession, credentials: HTTPAuthorizationCredentials | None) -> Device:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token required")
    token_payload = verify_node_token(credentials.credentials)
    if not token_payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token")
    try:
        device_id = UUID(str(token_payload.get("sub")))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token subject")
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


def _verify_signed(device: Device, timestamp: str, payload: dict[str, str], signature: str) -> None:
    if not device.public_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device has no public key")
    try:
        ensure_timestamp_fresh(timestamp, max_skew_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    try:
        verify_request_signature(device.public_key, payload, signature)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))


async def _consume_nonce(scope: str, nonce: str) -> None:
    if not await mark_nonce_used(scope, nonce, ttl_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Nonce already used")


# ── Device: poll for next job ─────────────────────────────────────────────────

@router.post("/device/jobs/next")
async def device_poll_next_job(
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
):
    """Device polls for its next dispatched-but-unaccepted job (with its lease)."""
    device = await _node_device(db, credentials)
    now = datetime.now(timezone.utc)

    if settings.LEASE_SWEEP_ON_POLL:
        await leases.sweep_expired(db, now, limit=25)

    job = (
        await db.execute(
            select(Job).join(JobAssignment)
            .where(JobAssignment.device_id == device.id, Job.status == JobStatus.DISPATCHED)
            .order_by(Job.priority.desc(), Job.created_at).limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        return {"job": None, "device_id": str(device.id), "status": device.status.value}

    # Defense in depth: never hand out work a device must not run right now
    # (e.g. it overheated after dispatch). The job stays DISPATCHED until its
    # accept lease runs out, then is requeued for another device.
    thresholds = await bench_svc.thresholds_for_device(db, device)
    blocked = dispatch_block_reason(device, job.workload_type, thresholds, now)
    if blocked:
        return {
            "job": None, "paused": True, "reason": blocked, "retry_after_seconds": 30,
            "device_id": str(device.id), "status": device.status.value,
        }

    lease = await leases.current_lease(db, job.id, device.id)
    lease_view = leases.lease_dict(lease)
    return {
        "job_id": str(job.id),
        "job": {
            "job_id": str(job.id),
            "workload_type": job.workload_type,
            "payload": _payload_of(job),
            "max_runtime_seconds": job.max_runtime_seconds,
            "created_at": job.created_at.isoformat(),
            "lease": lease_view,
        },
        "lease": lease_view,
        "device_id": str(device.id),
        "device_human_id": device.human_id,
    }


# ── Device: accept job ────────────────────────────────────────────────────────

@router.post("/device/jobs/{job_id}/accept")
async def device_accept_job(
    job_id: UUID,
    req: JobAcceptRequest,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
):
    """Device accepts a dispatched job; the lease switches to the runtime window."""
    device = await _node_device(db, credentials)
    _verify_signed(device, req.request_timestamp, {
        "request_nonce": req.request_nonce,
        "request_timestamp": req.request_timestamp,
        "job_id": str(job_id),
        "device_id": str(device.id),
    }, req.signature)
    now = datetime.now(timezone.utc)

    job = (
        await db.execute(
            select(Job).options(selectinload(Job.assignment)).join(JobAssignment)
            .where(Job.id == job_id, JobAssignment.device_id == device.id).with_for_update(of=Job)
        )
    ).scalar_one_or_none()
    if job is None:
        last = await leases.latest_lease(db, job_id, device.id)
        if last is not None and last.status == leases.EXPIRED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Lease expired; job was requeued")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found or not assigned to this device")

    lease = await leases.current_lease(db, job.id, device.id)
    # Idempotent: re-accepting a job this device already accepted returns 200.
    if job.status in {JobStatus.ACCEPTED, JobStatus.RUNNING}:
        return {"job_id": str(job.id), "status": job.status.value, "idempotent_replay": True,
                "accepted_at": job.assignment.accepted_at.isoformat() if job.assignment and job.assignment.accepted_at else None,
                "lease": leases.lease_dict(lease)}
    if job.status != JobStatus.DISPATCHED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Job not acceptable in status {job.status.value}")

    await _consume_nonce(f"accept:{device.id}", req.request_nonce)

    if lease is not None and not leases.is_live(lease, now):
        await leases.expire_lease(db, lease, now, reason="accept window elapsed")
        await db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Lease expired; job was requeued")

    thresholds = await bench_svc.thresholds_for_device(db, device)
    blocked = dispatch_block_reason(device, job.workload_type, thresholds, now)
    if blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Accept blocked by safety policy: {blocked}")

    if lease is None:  # job dispatched before leases existed
        lease = await leases.issue_lease(db, job, device.id, now)
    leases.on_accept(lease, job, now)
    if job.assignment:
        job.assignment.accepted_at = now
    leases.add_event(db, job.id, JobStatus.DISPATCHED.value, JobStatus.ACCEPTED.value,
                     reason=f"lease {lease.id} accepted", actor_type=JobEventActorType.NODE, actor_id=device.id)
    job.status = JobStatus.ACCEPTED
    job.updated_at = now

    # Device -> RUNNING only from schedulable states (a BENCHMARKING node keeps
    # its onboarding status while it benchmarks).
    await db.execute(
        update(Device)
        .where(Device.id == device.id, Device.status.in_(list(SCHEDULABLE_STATUSES)))
        .values(status=DeviceStatus.RUNNING, updated_at=now)
    )
    await db.commit()
    logger.info("job_accepted", job_id=str(job.id), device_id=str(device.id), lease_id=str(lease.id))
    return {"job_id": str(job.id), "status": JobStatus.ACCEPTED.value, "accepted_at": now.isoformat(),
            "idempotent_replay": False, "lease": leases.lease_dict(lease)}


# ── Device: renew lease ───────────────────────────────────────────────────────

@router.post("/device/jobs/{job_id}/lease/renew")
async def device_renew_lease(
    job_id: UUID,
    req: LeaseRenewRequest,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
):
    """Extend a running job's lease (capped at LEASE_MAX_TOTAL_SECONDS)."""
    device = await _node_device(db, credentials)
    _verify_signed(device, req.request_timestamp, {
        "request_nonce": req.request_nonce,
        "request_timestamp": req.request_timestamp,
        "job_id": str(job_id),
        "device_id": str(device.id),
        "lease_id": str(req.lease_id),
    }, req.signature)
    await _consume_nonce(f"renew:{device.id}", req.request_nonce)
    now = datetime.now(timezone.utc)

    from app.db.models.jobs_ledger import JobLease
    lease = (
        await db.execute(select(JobLease).where(JobLease.id == req.lease_id, JobLease.job_id == job_id,
                                                JobLease.device_id == device.id).with_for_update())
    ).scalar_one_or_none()
    if lease is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
    if lease.status == leases.ACTIVE and not leases.is_live(lease, now):
        await leases.expire_lease(db, lease, now)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Lease expired; job was requeued")
    try:
        leases.renew(lease, now)
    except leases.LeaseError as exc:
        raise HTTPException(status_code=exc.code, detail=str(exc))
    await db.commit()
    return {"job_id": str(job_id), "lease": leases.lease_dict(lease)}


# ── Device: submit result ─────────────────────────────────────────────────────

def _replay_response(job: Job, result_hash: str, outcome: str | None) -> dict[str, Any]:
    return {"job_id": str(job.id), "status": job.status.value, "result_hash": result_hash,
            "idempotent_replay": True, "attempt_outcome": outcome,
            "verification": _verification_view(job)}


@router.post("/device/jobs/{job_id}/result")
async def device_submit_result(
    job_id: UUID,
    req: JobResultSubmitRequest,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
):
    """Device submits a signed job result.

    Idempotent: resubmitting the same result_hash returns the original outcome;
    a different hash for an already-submitted attempt is rejected (409).
    Results on an expired lease are rejected and never credited (spec §71).
    With VERIFY_ON_SUBMIT the result is verified immediately and test credit is
    posted only if verification passes.
    """
    device = await _node_device(db, credentials)
    _verify_signed(device, req.request_timestamp, {
        "request_nonce": req.request_nonce,
        "request_timestamp": req.request_timestamp,
        "job_id": str(job_id),
        "device_id": str(device.id),
        "result_hash": req.result_hash,
        "result_uri": req.result_uri or "",
    }, req.signature)
    now = datetime.now(timezone.utc)
    result_hash = req.result_hash.lower()

    job = (
        await db.execute(
            select(Job)
            .options(selectinload(Job.assignment), selectinload(Job.result), selectinload(Job.verification))
            .where(Job.id == job_id).with_for_update(of=Job)
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # ── idempotency ──
    if job.result is not None and job.result.device_id == device.id:
        if job.result.result_hash.lower() == result_hash:
            return _replay_response(job, result_hash, "RESULT_SUBMITTED")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="A different result was already submitted for this attempt")
    last = await leases.latest_lease(db, job.id, device.id)
    if job.assignment is None or job.assignment.device_id != device.id:
        if last is not None and last.result_hash and last.result_hash.lower() == result_hash:
            return _replay_response(job, result_hash, last.outcome)
        if last is not None and last.status == leases.EXPIRED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="Lease expired; late result rejected (no credit)")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not assigned to this device")
    if job.status not in {JobStatus.ACCEPTED, JobStatus.RUNNING}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Job not in acceptable state ({job.status.value})")

    await _consume_nonce(f"result:{device.id}", req.request_nonce)

    lease = await leases.current_lease(db, job.id, device.id)
    if lease is not None and not leases.is_live(lease, now):
        await leases.expire_lease(db, lease, now, reason="result arrived after lease expiry")
        await db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Lease expired; late result rejected (no credit)")

    result = JobResult(job_id=job.id, device_id=device.id, result_hash=result_hash,
                       result_uri=req.result_uri, execution_metrics=req.execution_metrics)
    db.add(result)
    job.result = result
    leases.close_lease(lease, leases.COMPLETED, "RESULT_SUBMITTED", now, result_hash=result_hash)
    prev = job.status
    leases.add_event(db, job.id, prev.value, JobStatus.RESULT_SUBMITTED.value,
                     reason=f"result {result_hash[:16]}", actor_type=JobEventActorType.NODE, actor_id=device.id)
    job.status = JobStatus.RESULT_SUBMITTED
    job.updated_at = now
    if job.assignment:
        job.assignment.released_at = now
    # Work is done on-device: RUNNING -> ACTIVE (never override COOLING/QUARANTINED).
    await db.execute(
        update(Device).where(Device.id == device.id, Device.status == DeviceStatus.RUNNING)
        .values(status=DeviceStatus.ACTIVE, updated_at=now)
    )
    await db.flush()

    verification: dict[str, Any] | None = None
    if settings.VERIFY_ON_SUBMIT:
        verification = await vsvc.verify_and_finalize(db, job, result, now=now)
    await db.commit()

    logger.info("job_result_submitted", job_id=str(job.id), device_id=str(device.id), result_hash=result_hash,
                verification=(verification or {}).get("verification_status"))
    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "submitted_at": now.isoformat(),
        "idempotent_replay": False,
        "verification": verification,
    }


# ── FARMOS: verify result + credit earnings ───────────────────────────────────

class VerifyResultRequest(BaseModel):
    job_id: UUID
    auto_credit_test: bool = True  # credit test earnings if (and only if) verification passes
    resolve_pending: bool = False   # finish a VERIFYING job whose redundant sample is stuck


@router.post("/verify/result")
async def verify_and_credit_result(
    req: VerifyResultRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    """Verify a submitted result (idempotent).

    Results are normally verified automatically on submission; calling this for
    an already-verified job returns the recorded outcome without re-crediting.
    """
    job = await vsvc._load_job(db, req.job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    now = datetime.now(timezone.utc)

    if job.status == JobStatus.RESULT_SUBMITTED and job.result is not None:
        out = await vsvc.verify_and_finalize(db, job, job.result, actor_id=current_user.id,
                                             auto_credit_test=req.auto_credit_test, now=now)
        await db.commit()
        return {"job_id": str(job.id), "status": job.status.value, "is_valid": out["verification_status"] == vsvc.PASSED,
                "idempotent_replay": False, "verification": out,
                "test_earnings_credited": out["test_earnings_credited"], "real_earnings_credited": False,
                "test_account_type": LedgerAccountType.DEVICE_EARNINGS_TEST.value,
                "real_account_type": LedgerAccountType.DEVICE_EARNINGS_REAL.value}

    if job.status == JobStatus.VERIFYING and req.resolve_pending:
        out = await vsvc.resolve_orphaned_primary(db, job.id, now)
        await db.commit()
        job = await vsvc._load_job(db, req.job_id)

    view = _verification_view(job)
    if view is None and job.status not in {JobStatus.VERIFYING}:
        last = await leases.latest_lease(db, job.id)
        if last is None or not (last.evidence or {}).get("verification"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Job not in verifiable state (current={job.status.value})")
        ev = last.evidence["verification"]
        view = {"status": "FAILED", "method": ev.get("method"), "confidence": ev.get("confidence"),
                "reason": ev.get("reason"), "verified_at": last.closed_at.isoformat() if last.closed_at else None}
    from app.services.job_executor import job_already_credited
    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "is_valid": {vsvc.PASSED: True, vsvc.FAILED: False}.get((view or {}).get("status")),
        "idempotent_replay": True,
        "verification": view,
        "test_earnings_credited": await job_already_credited(db, job.id),
        "real_earnings_credited": False,
        "test_account_type": LedgerAccountType.DEVICE_EARNINGS_TEST.value,
        "real_account_type": LedgerAccountType.DEVICE_EARNINGS_REAL.value,
    }


# ── Device earnings summary ───────────────────────────────────────────────────

@router.get("/device/{device_id}/earnings")
async def get_device_earnings(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    """Get earnings summary for a device."""
    device_q = await db.execute(select(Device).where(Device.id == device_id))
    device = device_q.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    test_accounts = await db.execute(
        select(LedgerAccount).where(
            LedgerAccount.owner_type == "DEVICE",
            LedgerAccount.owner_id == device_id,
            LedgerAccount.account_type == LedgerAccountType.DEVICE_EARNINGS_TEST,
        )
    )
    test_account = test_accounts.scalar_one_or_none()

    real_accounts = await db.execute(
        select(LedgerAccount).where(
            LedgerAccount.owner_type == "DEVICE",
            LedgerAccount.owner_id == device_id,
            LedgerAccount.account_type == LedgerAccountType.DEVICE_EARNINGS_REAL,
        )
    )
    real_account = real_accounts.scalar_one_or_none()

    async def sum_entries(account) -> dict[str, float]:
        if not account:
            return {"test_usd": 0.0, "real_usdc": 0.0}
        entries_q = await db.execute(
            select(LedgerEntry).where(
                LedgerEntry.account_id == account.id,
                LedgerEntry.direction == LedgerDirection.CREDIT,
            )
        )
        entries = entries_q.scalars().all()
        test_total = 0.0
        real_total = 0.0
        for e in entries:
            amt = float(e.amount)
            if e.asset == "USD":
                test_total += amt
            elif e.asset == "USDC":
                real_total += amt
        return {"test_usd": round(test_total, 8), "real_usdc": round(real_total, 8)}

    test_summary = await sum_entries(test_account)
    real_summary = await sum_entries(real_account)

    return {
        "device_id": str(device.id),
        "device_human_id": device.human_id,
        "status": device.status.value,
        "test_earnings": {
            "asset": "USD",
            "balance_usd": test_summary["test_usd"],
            "account_type": LedgerAccountType.DEVICE_EARNINGS_TEST.value,
            "note": "Test credits only — not real money",
        },
        "real_earnings": {
            "asset": "USDC",
            "balance_usdc": real_summary["real_usdc"],
            "account_type": LedgerAccountType.DEVICE_EARNINGS_REAL.value,
            "note": "Real USDC earnings — requires verified customer payment",
        },
    }
