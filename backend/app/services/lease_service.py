"""Job leases, timeout requeue, retry limits and dead-letter (spec §69, §70, §71).

* Every dispatch issues a lease (lease_id, device_id, issued/expiry, grace).
* A DISPATCHED lease must be accepted within LEASE_ACCEPT_TIMEOUT_SECONDS; on
  accept the expiry becomes now + max_runtime + buffer.
* Devices renew long-running leases; renewals are capped at
  LEASE_MAX_TOTAL_SECONDS from acceptance.
* ``sweep_expired`` expires leases past expiry+grace and requeues the job
  (X -> EXPIRED -> QUEUED) or, when retries are exhausted, dead-letters it
  (EXPIRED -> FAILED, dead_lettered=True). Expired leases are never silently
  resumed (§70) and late results never receive credit (§71).
* job_leases rows double as the attempt history: the current attempt's
  assignment / result / verification rows are archived into the lease and
  deleted so a new attempt can be created (those tables are 1:1 with a job).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.config import settings
from app.db.models.devices import Device
from app.db.models.enums import DeviceStatus, JobEventActorType, JobStatus
from app.db.models.jobs_ledger import (
    Job,
    JobAssignment,
    JobEvent,
    JobLease,
    JobResult,
    JobVerification,
)
from app.logging import get_logger

logger = get_logger(__name__)

ACTIVE = "ACTIVE"
COMPLETED = "COMPLETED"
EXPIRED = "EXPIRED"
RELEASED = "RELEASED"

# Job states in which a device holds a live lease.
LEASED_STATES = {JobStatus.DISPATCHED, JobStatus.ACCEPTED, JobStatus.RUNNING}


class LeaseError(Exception):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def runtime_seconds(job: Job) -> int:
    return int(job.max_runtime_seconds or settings.LEASE_DEFAULT_RUNTIME_SECONDS)


def is_live(lease: JobLease, now: datetime) -> bool:
    return lease.status == ACTIVE and now <= _aware(lease.expires_at) + timedelta(seconds=lease.grace_seconds)


def lease_dict(lease: Optional[JobLease]) -> Optional[dict[str, Any]]:
    if lease is None:
        return None
    return {
        "lease_id": str(lease.id),
        "attempt": lease.attempt,
        "status": lease.status,
        "issued_at": _aware(lease.issued_at).isoformat(),
        "accepted_at": _aware(lease.accepted_at).isoformat() if lease.accepted_at else None,
        "expires_at": _aware(lease.expires_at).isoformat(),
        "grace_seconds": lease.grace_seconds,
        "renewals": lease.renewals,
        "renew_every_seconds": max(15, settings.LEASE_RENEW_SECONDS // 2),
        "outcome": lease.outcome,
    }


def add_event(db: AsyncSession, job_id: uuid.UUID, from_status: Optional[str], to_status: str,
              reason: str | None = None, actor_type: JobEventActorType = JobEventActorType.SYSTEM,
              actor_id: uuid.UUID | None = None) -> None:
    """Every transition creates an immutable job_event (spec §60)."""
    db.add(JobEvent(job_id=job_id, from_status=from_status, to_status=to_status,
                    reason=reason, actor_type=actor_type, actor_id=actor_id))


async def issue_lease(db: AsyncSession, job: Job, device_id: uuid.UUID, now: datetime | None = None) -> JobLease:
    now = now or utcnow()
    lease = JobLease(
        id=uuid.uuid4(),
        job_id=job.id,
        device_id=device_id,
        attempt=int(job.retry_count or 0) + 1,
        status=ACTIVE,
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.LEASE_ACCEPT_TIMEOUT_SECONDS),
        grace_seconds=settings.LEASE_GRACE_SECONDS,
        renewals=0,
        evidence={},
    )
    db.add(lease)
    await db.flush()
    return lease


async def current_lease(db: AsyncSession, job_id: uuid.UUID, device_id: uuid.UUID | None = None,
                        statuses: tuple[str, ...] = (ACTIVE,)) -> Optional[JobLease]:
    q = select(JobLease).where(JobLease.job_id == job_id, JobLease.status.in_(statuses))
    if device_id is not None:
        q = q.where(JobLease.device_id == device_id)
    return (await db.execute(q.order_by(JobLease.attempt.desc()).limit(1))).scalars().first()


async def latest_lease(db: AsyncSession, job_id: uuid.UUID, device_id: uuid.UUID | None = None) -> Optional[JobLease]:
    q = select(JobLease).where(JobLease.job_id == job_id)
    if device_id is not None:
        q = q.where(JobLease.device_id == device_id)
    return (await db.execute(q.order_by(JobLease.attempt.desc(), JobLease.issued_at.desc()).limit(1))).scalars().first()


def on_accept(lease: JobLease, job: Job, now: datetime | None = None) -> JobLease:
    now = now or utcnow()
    lease.accepted_at = now
    lease.expires_at = now + timedelta(seconds=runtime_seconds(job) + settings.LEASE_RUNTIME_BUFFER_SECONDS)
    return lease


def renew(lease: JobLease, now: datetime | None = None) -> JobLease:
    now = now or utcnow()
    if lease.status != ACTIVE:
        raise LeaseError(f"lease is {lease.status}")
    if not is_live(lease, now):
        raise LeaseError("lease expired; job will be requeued")
    if lease.accepted_at is None:
        raise LeaseError("lease not accepted yet")
    cap = _aware(lease.accepted_at) + timedelta(seconds=settings.LEASE_MAX_TOTAL_SECONDS)
    new_expiry = min(now + timedelta(seconds=settings.LEASE_RENEW_SECONDS), cap)
    if new_expiry <= _aware(lease.expires_at):
        if now >= cap:
            raise LeaseError("lease reached maximum total duration")
        return lease  # already further out; renewal is a no-op
    lease.expires_at = new_expiry
    lease.renewals = int(lease.renewals or 0) + 1
    return lease


def close_lease(lease: Optional[JobLease], status: str, outcome: str, now: datetime | None = None,
                result_hash: str | None = None, evidence: dict[str, Any] | None = None) -> None:
    if lease is None:
        return
    lease.status = status
    lease.outcome = outcome
    lease.closed_at = now or utcnow()
    if result_hash is not None:
        lease.result_hash = result_hash
    if evidence:
        lease.evidence = {**(lease.evidence or {}), **evidence}


async def _release_device(db: AsyncSession, device_id: uuid.UUID, to: DeviceStatus = DeviceStatus.IDLE) -> None:
    # Only RUNNING is ours to release; never override COOLING/THROTTLED/QUARANTINED.
    await db.execute(
        update(Device).where(Device.id == device_id, Device.status == DeviceStatus.RUNNING)
        .values(status=to, updated_at=utcnow())
    )


async def clear_attempt(db: AsyncSession, job: Job) -> None:
    """Delete the current attempt's 1:1 rows (already archived in the lease)."""
    for model in (JobVerification, JobResult, JobAssignment):
        await db.execute(delete(model).where(model.job_id == job.id))
    # Keep the in-memory relationship state consistent with the deletes.
    for rel in ("assignment", "result", "verification"):
        set_committed_value(job, rel, None)


async def requeue_or_dead_letter(db: AsyncSession, job: Job, from_status: JobStatus, reason: str,
                                 now: datetime | None = None, failed_device_id: uuid.UUID | None = None) -> JobStatus:
    """Move a failed attempt to QUEUED (retry) or FAILED + dead_lettered."""
    now = now or utcnow()
    await clear_attempt(db, job)
    job.retry_count = int(job.retry_count or 0) + 1
    job.last_error = reason[:2000]
    spec = dict(job.verification_spec or {})
    if failed_device_id is not None:
        excluded = list(spec.get("excluded_devices", []))
        if str(failed_device_id) not in excluded:
            excluded.append(str(failed_device_id))
        spec["excluded_devices"] = excluded[-20:]
        job.verification_spec = spec

    if job.retry_count > int(job.max_retries if job.max_retries is not None else settings.JOB_DEFAULT_MAX_RETRIES):
        job.status = JobStatus.FAILED
        job.dead_lettered = True
        add_event(db, job.id, from_status.value, JobStatus.FAILED.value,
                  reason=f"dead-letter: retries exhausted ({job.retry_count - 1}/{job.max_retries}); last error: {reason}"[:2000])
        logger.warning("job_dead_lettered", job_id=str(job.id), retries=job.retry_count - 1, reason=reason)
    else:
        job.status = JobStatus.QUEUED
        add_event(db, job.id, from_status.value, JobStatus.QUEUED.value,
                  reason=f"requeued for retry {job.retry_count}/{job.max_retries}: {reason}"[:2000])
        logger.info("job_requeued", job_id=str(job.id), retry=job.retry_count, reason=reason)
    job.updated_at = now
    return job.status


async def expire_lease(db: AsyncSession, lease: JobLease, now: datetime | None = None,
                       reason: str = "lease expired") -> Optional[JobStatus]:
    """Expire one lease and requeue/dead-letter its job. Caller commits."""
    from app.services import reputation_service as rep

    now = now or utcnow()
    job = (await db.execute(select(Job).where(Job.id == lease.job_id).with_for_update())).scalar_one_or_none()
    close_lease(lease, EXPIRED, "TIMEOUT", now)
    if job is None or job.status not in LEASED_STATES:
        return job.status if job else None

    prev = job.status
    add_event(db, job.id, prev.value, JobStatus.EXPIRED.value,
              reason=f"{reason} (lease {lease.id}, attempt {lease.attempt})")
    await _release_device(db, lease.device_id)
    device = (await db.execute(select(Device).where(Device.id == lease.device_id))).scalar_one_or_none()
    # Only penalise devices that accepted and then went silent; an un-accepted
    # dispatch (device offline/paused) is a scheduling miss, not misbehaviour.
    if device is not None and lease.accepted_at is not None:
        await rep.record_outcome(db, device, rep.TIMEOUT, job_id=job.id,
                                 detail=f"lease expired for job {job.id}", now=now)
    new_status = await requeue_or_dead_letter(db, job, JobStatus.EXPIRED, reason, now,
                                              failed_device_id=lease.device_id)

    # A shadow (redundant) job that cannot complete must not leave its primary
    # stuck in VERIFYING.
    if new_status == JobStatus.FAILED and (job.verification_spec or {}).get("role") == "REDUNDANT" and job.parent_job_id:
        from app.services import verification_service as vsvc
        await vsvc.resolve_orphaned_primary(db, job.parent_job_id, now)
    return new_status


async def sweep_expired(db: AsyncSession, now: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Expire every ACTIVE lease past expiry + grace. Commits."""
    now = now or utcnow()
    leases = (
        await db.execute(
            select(JobLease).where(JobLease.status == ACTIVE, JobLease.expires_at < now)
            .order_by(JobLease.expires_at).limit(limit)
        )
    ).scalars().all()
    swept: list[dict[str, Any]] = []
    for lease in leases:
        if is_live(lease, now):
            continue  # inside grace
        new_status = await expire_lease(db, lease, now)
        swept.append({"lease_id": str(lease.id), "job_id": str(lease.job_id),
                      "device_id": str(lease.device_id),
                      "job_status": new_status.value if new_status else None})
    if swept:
        await db.commit()
    return swept
