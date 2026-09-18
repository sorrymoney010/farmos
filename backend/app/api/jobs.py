"""FARMOS job endpoints — dispatch, device accept, result submission, verification, device polling."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.enums import (
    DeviceStatus,
    JobEventActorType,
    JobStatus,
    JobType,
    LedgerAccountType,
    LedgerDirection,
)
from app.db.models.devices import Device
from app.db.models.jobs_ledger import (
    Job,
    JobAssignment,
    JobEvent,
    JobResult,
    JobVerification,
    LedgerAccount,
    LedgerEntry,
)
from app.db.session import get_db
from app.dependencies import get_current_user, require_roles
from app.logging import get_logger
from app.security.node_auth import verify_node_token, verify_request_signature, ensure_timestamp_fresh

logger = get_logger(__name__)
router = APIRouter()


# ── Shared request models ─────────────────────────────────────────────────────

class JobCreateRequest(BaseModel):
    workload_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_revenue_usd: float | None = None
    expected_cost_usd: float | None = None
    max_runtime_seconds: int | None = None
    priority: int = 100
    device_id: UUID | None = None  # optional: dispatch to specific device


class JobDispatchRequest(BaseModel):
    job_id: UUID
    device_id: UUID


class JobAcceptRequest(BaseModel):
    request_nonce: str
    request_timestamp: str
    signature: str


class JobResultSubmitRequest(BaseModel):
    request_nonce: str
    request_timestamp: str
    signature: str
    result_hash: str
    result_uri: str | None = None
    execution_metrics: dict[str, Any] = Field(default_factory=dict)


# ── Helper: ensure job exists and belongs to requester ────────────────────────

async def _get_job_for_user(db: AsyncSession, job_id: UUID, user_id: UUID) -> Job:
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.customer_id == user_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


# ── Create job ────────────────────────────────────────────────────────────────

@router.post("/")
async def create_job(
    req: JobCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "CUSTOMER")),
):
    job_type = req.workload_type
    try:
        JobType(job_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported workload_type: {job_type}",
        )

    job = Job(
        workload_type=job_type,
        status=JobStatus.CREATED,
        priority=req.priority,
        requested_resources=req.payload,
        expected_revenue_usd=req.expected_revenue_usd,
        expected_cost_usd=req.expected_cost_usd,
        max_runtime_seconds=req.max_runtime_seconds,
        adapter_id="internal",
        customer_id=current_user.id,
    )
    db.add(job)
    await db.flush()

    event = JobEvent(
        job_id=job.id,
        from_status=None,
        to_status=JobStatus.CREATED.value,
        actor_type=JobEventActorType.USER,
        actor_id=current_user.id,
    )
    db.add(event)
    await db.commit()

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "workload_type": job.workload_type,
        "priority": job.priority,
        "created_at": job.created_at.isoformat(),
    }


# ── List jobs ─────────────────────────────────────────────────────────────────

@router.get("/")
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS", "CUSTOMER")),
):
    result = await db.execute(
        select(Job).order_by(desc(Job.created_at)).limit(100)
    )
    jobs = result.scalars().all()
    return [
        {
            "job_id": str(j.id),
            "status": j.status.value,
            "workload_type": j.workload_type,
            "priority": j.priority,
            "created_at": j.created_at.isoformat(),
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
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    payload: dict[str, Any] = {}
    if job.requested_resources:
        try:
            payload = json.loads(job.requested_resources) if isinstance(job.requested_resources, str) else dict(job.requested_resources)
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {"raw": str(job.requested_resources)}

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "workload_type": job.workload_type,
        "priority": job.priority,
        "payload": payload,
        "expected_revenue_usd": float(job.expected_revenue_usd) if job.expected_revenue_usd else None,
        "expected_cost_usd": float(job.expected_cost_usd) if job.expected_cost_usd else None,
        "max_runtime_seconds": job.max_runtime_seconds,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
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
    job_result = await db.execute(select(Job).where(Job.id == req.job_id))
    job = job_result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status not in {JobStatus.CREATED, JobStatus.QUEUED}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job cannot be dispatched in status {job.status.value}",
        )

    device_result = await db.execute(select(Device).where(Device.id == req.device_id))
    device = device_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if device.status != DeviceStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Device not active (status={device.status.value})",
        )

    # Create assignment
    assignment = JobAssignment(
        job_id=job.id,
        device_id=req.device_id,
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(assignment)

    # Update job status
    await db.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(status=JobStatus.DISPATCHED, updated_at=datetime.now(timezone.utc))
    )

    event = JobEvent(
        job_id=job.id,
        from_status=job.status.value,
        to_status=JobStatus.DISPATCHED.value,
        actor_type=JobEventActorType.SYSTEM,
        actor_id=None,
    )
    db.add(event)
    await db.commit()

    logger.info(
        "job_dispatched",
        job_id=str(job.id),
        device_id=str(device.id),
        device_human_id=device.human_id,
    )

    return {
        "job_id": str(job.id),
        "device_id": str(device.id),
        "device_human_id": device.human_id,
        "status": JobStatus.DISPATCHED.value,
        "assigned_at": assignment.assigned_at.isoformat(),
    }


# ── Device: poll for next job ─────────────────────────────────────────────────

node_bearer = HTTPBearer(auto_error=False)


@router.post("/device/jobs/next")
async def device_poll_next_job(
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
):
    """Device polls for its next dispatched-but-unaccepted job.

    Requires valid node token in Authorization: Bearer <node_token>.
    Returns the job payload if one is available, or {"job": null}.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token required")

    token_payload = verify_node_token(credentials.credentials)
    if not token_payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token")

    device_id_str = token_payload.get("sub")
    if not device_id_str:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token missing subject")

    try:
        device_id: UUID = UUID(device_id_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token subject")

    device_result = await db.execute(select(Device).where(Device.id == device_id))
    device = device_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    # Find the oldest dispatched job for this device that hasn't been accepted yet
    job_q = await db.execute(
        select(Job)
        .join(JobAssignment)
        .where(
            JobAssignment.device_id == device_id,
            Job.status == JobStatus.DISPATCHED,
        )
        .order_by(Job.created_at)
        .limit(1)
    )
    job = job_q.scalar_one_or_none()

    if not job:
        return {"job": None, "device_id": str(device.id), "status": device.status.value}

    # Parse payload
    payload: dict[str, Any] = {}
    if job.requested_resources:
        try:
            payload = json.loads(job.requested_resources) if isinstance(job.requested_resources, str) else dict(job.requested_resources)
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {"raw": str(job.requested_resources)}

    return {
        "job_id": str(job.id),
        "job": {
            "job_id": str(job.id),
            "workload_type": job.workload_type,
            "payload": payload,
            "max_runtime_seconds": job.max_runtime_seconds,
            "created_at": job.created_at.isoformat(),
        },
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
    """Device accepts a dispatched job."""
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token required")

    token_payload = verify_node_token(credentials.credentials)
    if not token_payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token")

    device_id_str = token_payload.get("sub")
    try:
        device_id: UUID = UUID(device_id_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token subject")

    # Verify timestamp freshness
    try:
        ensure_timestamp_fresh(req.request_timestamp, max_skew_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Verify signature
    payload_to_verify: dict[str, str] = {
        "request_nonce": req.request_nonce,
        "request_timestamp": req.request_timestamp,
        "job_id": str(job_id),
        "device_id": device_id_str,
    }
    try:
        verify_request_signature(token_payload.get("pubkey", ""), payload_to_verify, req.signature)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    # Find job assignment
    job_q = await db.execute(
        select(Job)
        .join(JobAssignment)
        .where(
            Job.id == job_id,
            JobAssignment.device_id == device_id,
            Job.status == JobStatus.DISPATCHED,
        )
    )
    job = job_q.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found or not assigned to this device")

    # Mark accepted
    await db.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(status=JobStatus.ACCEPTED, updated_at=datetime.now(timezone.utc))
    )
    if job.assignment:
        await db.execute(
            update(JobAssignment)
            .where(JobAssignment.job_id == job.id)
            .values(accepted_at=datetime.now(timezone.utc))
        )

    event = JobEvent(
        job_id=job.id,
        from_status=JobStatus.DISPATCHED.value,
        to_status=JobStatus.ACCEPTED.value,
        actor_type=JobEventActorType.NODE,
        actor_id=device_id,
    )
    db.add(event)

    # Update device status to RUNNING
    await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .values(status=DeviceStatus.RUNNING, updated_at=datetime.now(timezone.utc))
    )

    await db.commit()

    logger.info("job_accepted", job_id=str(job.id), device_id=str(device_id))

    return {
        "job_id": str(job.id),
        "status": JobStatus.ACCEPTED.value,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Device: submit result ─────────────────────────────────────────────────────

@router.post("/device/jobs/{job_id}/result")
async def device_submit_result(
    job_id: UUID,
    req: JobResultSubmitRequest,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
):
    """Device submits a signed job result."""
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token required")

    token_payload = verify_node_token(credentials.credentials)
    if not token_payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token")

    device_id_str = token_payload.get("sub")
    try:
        device_id: UUID = UUID(device_id_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token subject")

    # Verify timestamp freshness
    try:
        ensure_timestamp_fresh(req.request_timestamp, max_skew_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Verify signature
    payload_to_verify: dict[str, str] = {
        "request_nonce": req.request_nonce,
        "request_timestamp": req.request_timestamp,
        "job_id": str(job_id),
        "device_id": device_id_str,
        "result_hash": req.result_hash,
        "result_uri": req.result_uri or "",
    }
    try:
        verify_request_signature(token_payload.get("pubkey", ""), payload_to_verify, req.signature)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    # Find job
    job_q = await db.execute(
        select(Job)
        .join(JobAssignment)
        .where(
            Job.id == job_id,
            JobAssignment.device_id == device_id,
            Job.status.in_([JobStatus.ACCEPTED, JobStatus.RUNNING]),
        )
    )
    job = job_q.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found or not in acceptable state")

    # Create job result
    result = JobResult(
        job_id=job.id,
        device_id=device_id,
        result_hash=req.result_hash,
        result_uri=req.result_uri,
        execution_metrics=req.execution_metrics,
    )
    db.add(result)

    # Update job status to RESULT_SUBMITTED
    await db.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(status=JobStatus.RESULT_SUBMITTED, updated_at=datetime.now(timezone.utc))
    )

    event = JobEvent(
        job_id=job.id,
        from_status=job.status.value,
        to_status=JobStatus.RESULT_SUBMITTED.value,
        actor_type=JobEventActorType.NODE,
        actor_id=device_id,
    )
    db.add(event)

    await db.commit()

    logger.info(
        "job_result_submitted",
        job_id=str(job.id),
        device_id=str(device_id),
        result_hash=req.result_hash,
    )

    return {
        "job_id": str(job.id),
        "status": JobStatus.RESULT_SUBMITTED.value,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


# ── FARMOS: verify result + credit earnings ───────────────────────────────────

class VerifyResultRequest(BaseModel):
    job_id: UUID
    auto_credit_test: bool = True  # auto-credit test earnings on verification


@router.post("/verify/result")
async def verify_and_credit_result(
    req: VerifyResultRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS")),
):
    """FARMOS verifies a submitted result and optionally credits test earnings.

    After real customer payment is verified, call with is_real_revenue=True
    to credit the device's REAL earnings ledger.
    """
    job_q = await db.execute(select(Job).where(Job.id == req.job_id))
    job = job_q.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.status not in {JobStatus.RESULT_SUBMITTED, JobStatus.VERIFYING}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job not in verifiable state (current={job.status.value})",
        )

    # Get the submitted result
    result_q = await db.execute(
        select(JobResult).where(JobResult.job_id == job.id)
    )
    job_result_obj = result_q.scalar_one_or_none()
    if not job_result_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No result submitted for this job")

    # Verify
    from app.services.job_executor import verify_result as svc_verify

    is_valid = await svc_verify(db, job, job_result_obj)

    verification = JobVerification(
        job_id=job.id,
        status="PASSED" if is_valid else "FAILED",
        method="automatic",
        evidence={
            "result_hash": job_result_obj.result_hash,
            "is_valid": is_valid,
        },
        verified_at=datetime.now(timezone.utc),
    )
    db.add(verification)

    new_status = JobStatus.VERIFIED if is_valid else JobStatus.INVALID_RESULT
    await db.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(status=new_status, updated_at=datetime.now(timezone.utc))
    )

    event = JobEvent(
        job_id=job.id,
        from_status=job.status.value,
        to_status=new_status.value,
        actor_type=JobEventActorType.SYSTEM,
        actor_id=current_user.id,
    )
    db.add(event)

    # Credit test earnings if valid and auto_credit_test is True
    credited_test = False
    credited_real = False
    if is_valid and req.auto_credit_test:
        from app.services.job_executor import credit_device_earnings as svc_credit

        entry = await svc_credit(db, job, job_result_obj.device_id, job_result_obj, is_real_revenue=False)
        credited_test = True

    await db.commit()

    # Update device status back to ACTIVE if it was RUNNING
    if job.assignment:
        await db.execute(
            update(Device)
            .where(Device.id == job.assignment.device_id)
            .values(status=DeviceStatus.ACTIVE, updated_at=datetime.now(timezone.utc))
        )
        await db.commit()

    return {
        "job_id": str(job.id),
        "status": new_status.value,
        "verified_at": verification.verified_at.isoformat(),
        "is_valid": is_valid,
        "test_earnings_credited": credited_test,
        "real_earnings_credited": credited_real,
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
