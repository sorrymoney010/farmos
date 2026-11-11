"""Independent result verification (spec §57.6, §67) and credit-after-verification.

Policy per workload (spec §67 examples):

* checksum / json_transform: HASH + DETERMINISTIC_RECOMPUTE (server recomputes
  the exact answer; any mismatch is a hard failure).
* compression: HASH + ROUNDTRIP (decompress, compare sha256 of the
  deterministic input; independent of zlib version).
* http_check / api_request / monitor: SCHEMA + REDUNDANT_SAMPLE. A deterministic
  sample of primaries (REDUNDANT_SAMPLE_RATE) is re-executed as a shadow job on a
  *different* eligible device and the observations compared. If no second
  device is available the job falls back to SCHEMA with LOW confidence.
* CANARY (known-answer spot-check): admin-created jobs whose hidden expected
  answer lives in ``Job.verification_spec`` (never sent to devices).

Effects (``finalize``): PASSED -> VERIFIED, reputation +, test credit for
PRIMARY jobs only (idempotent). FAILED -> INVALID_RESULT (or FAILED when the
device itself reported failure) then requeue or dead-letter, reputation -,
auto-quarantine on repeated failures. Nothing is ever credited before
verification passes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models.devices import Device
from app.db.models.enums import JobStatus, JobType
from app.db.models.jobs_ledger import Job, JobAssignment, JobResult, JobVerification
from app.logging import get_logger
from app.services import lease_service as leases
from app.services import reputation_service as rep
from app.workloads import cpu_data

logger = get_logger(__name__)

PRIMARY = "PRIMARY"
REDUNDANT = "REDUNDANT"
CANARY = "CANARY"

PASSED = "PASSED"
FAILED = "FAILED"
PENDING = "PENDING"

DETERMINISTIC = {JobType.CHECKSUM.value, JobType.JSON_TRANSFORM.value, JobType.COMPRESSION.value}
OBSERVATIONAL = {JobType.HTTP_CHECK.value, JobType.API_REQUEST.value, JobType.MONITOR.value}

VERIFICATION_POLICY: dict[str, list[str]] = {
    JobType.CHECKSUM.value: ["HASH", "DETERMINISTIC_RECOMPUTE"],
    JobType.JSON_TRANSFORM.value: ["HASH", "DETERMINISTIC_RECOMPUTE"],
    JobType.COMPRESSION.value: ["HASH", "ROUNDTRIP_RECOMPUTE"],
    JobType.HTTP_CHECK.value: ["SCHEMA", "REDUNDANT_SAMPLE"],
    JobType.API_REQUEST.value: ["SCHEMA", "REDUNDANT_SAMPLE"],
    JobType.MONITOR.value: ["SCHEMA", "REDUNDANT_SAMPLE"],
    JobType.DATA_TRANSFORM.value: ["SCHEMA"],
}


@dataclass
class Outcome:
    status: str                      # PASSED | FAILED | PENDING
    method: str
    confidence: str = "HIGH"         # HIGH | MEDIUM | LOW
    reason: str = ""
    penalty: Optional[str] = None    # reputation kind on failure
    device_failed: bool = False      # device itself reported FAILED
    evidence: dict[str, Any] = field(default_factory=dict)


# ── pure helpers ─────────────────────────────────────────────────────────────

def role_of(job: Job) -> str:
    return str((job.verification_spec or {}).get("role", PRIMARY))


def job_payload(job: Job) -> dict[str, Any]:
    p = job.requested_resources
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            return {}
    return dict(p) if isinstance(p, dict) else {}


def parse_result(result: JobResult) -> Optional[dict[str, Any]]:
    if not result.result_uri:
        return None
    try:
        data = json.loads(result.result_uri)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def hash_matches(obj: dict[str, Any], result_hash: str) -> bool:
    """result_hash = sha256(canonical JSON). Accept the normalized-number form
    (current agents) and the raw json.dumps(sort_keys) form (older agents)."""
    h = (result_hash or "").lower()
    candidates = {
        cpu_data.sha256_hex(cpu_data.canonical(obj)),
        cpu_data.sha256_hex(json.dumps(obj, sort_keys=True, separators=(",", ":"))),
    }
    return h in candidates


def sampled_for_redundancy(job_id: uuid.UUID, rate: float) -> bool:
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    bucket = int(hashlib.sha256(str(job_id).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def check_schema(workload: str, obj: dict[str, Any]) -> tuple[bool, str]:
    def _http_ok(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and 100 <= v < 600

    if workload in (JobType.HTTP_CHECK.value, JobType.API_REQUEST.value):
        if not _http_ok(obj.get("http_status")):
            return False, "http_status missing or out of range"
        d = obj.get("duration_ms")
        if d is not None and (not isinstance(d, (int, float)) or isinstance(d, bool) or d < 0 or d > 3_600_000):
            return False, "duration_ms implausible"
        return True, "schema ok"
    if workload == JobType.MONITOR.value:
        t = obj.get("targets_checked")
        return (isinstance(t, int) and t > 0), "targets_checked must be > 0"
    if workload == JobType.DATA_TRANSFORM.value:
        return ("output" in obj), "output missing"
    return True, "no schema"


def check_deterministic(workload: str, payload: dict[str, Any], obj: dict[str, Any]) -> Outcome:
    if workload == JobType.COMPRESSION.value:
        ok, why = cpu_data.verify_compression(payload, obj)
        return Outcome(PASSED if ok else FAILED, "HASH+ROUNDTRIP_RECOMPUTE", reason=why,
                       penalty=None if ok else rep.FAIL)
    expected = cpu_data.execute(workload, payload)
    if expected.get("status") != "COMPLETED":
        # The job itself is malformed; a device that also refused it is right.
        return Outcome(FAILED, "DETERMINISTIC_RECOMPUTE", reason=f"invalid job payload: {expected.get('error')}",
                       penalty=None, evidence={"job_invalid": True})
    keys = ["output_sha256", "size_bytes", "rounds"] if workload == JobType.CHECKSUM.value else ["output_sha256"]
    mismatched = [k for k in keys if obj.get(k) != expected.get(k)]
    if workload == JobType.JSON_TRANSFORM.value and cpu_data.canonical(obj.get("output")) != cpu_data.canonical(expected.get("output")):
        mismatched.append("output")
    if mismatched:
        return Outcome(FAILED, "HASH+DETERMINISTIC_RECOMPUTE", reason=f"recompute mismatch: {', '.join(mismatched)}",
                       penalty=rep.FAIL, evidence={"expected_sha256": expected.get("output_sha256"),
                                                   "reported_sha256": obj.get("output_sha256")})
    return Outcome(PASSED, "HASH+DETERMINISTIC_RECOMPUTE", reason="recomputed answer matches",
                   evidence={"output_sha256": expected.get("output_sha256")})


def check_canary(spec: dict[str, Any], obj: dict[str, Any]) -> Outcome:
    expected = spec.get("expected") or {}
    wrong = [k for k, v in expected.items() if obj.get(k) != v]
    if wrong:
        return Outcome(FAILED, "KNOWN_ANSWER", reason=f"canary mismatch: {', '.join(sorted(wrong))}",
                       penalty=rep.CANARY_FAIL)
    return Outcome(PASSED, "KNOWN_ANSWER", reason="known answer matched")


def compare_redundant(workload: str, primary: dict[str, Any], shadow: dict[str, Any]) -> tuple[bool, str]:
    if primary.get("status") != shadow.get("status"):
        return False, f"status differs ({primary.get('status')} vs {shadow.get('status')})"
    if workload in (JobType.HTTP_CHECK.value, JobType.API_REQUEST.value):
        a, b = primary.get("http_status"), shadow.get("http_status")
        if not isinstance(a, int) or not isinstance(b, int) or a // 100 != b // 100:
            return False, f"http_status class differs ({a} vs {b})"
        return True, f"http_status class agrees ({a} / {b})"
    if workload == JobType.MONITOR.value:
        if primary.get("targets_checked") != shadow.get("targets_checked"):
            return False, "targets_checked differs"
        return True, "targets agree"
    return True, "no comparator"


def evaluate_static(job: Job, result: JobResult) -> Outcome:
    """Everything except redundant sampling (which needs the DB)."""
    workload = job.workload_type
    spec = job.verification_spec or {}
    obj = parse_result(result)
    if obj is None:
        return Outcome(FAILED, "PARSE", reason="result_uri is not a JSON object", penalty=rep.FAIL)

    hash_ok = hash_matches(obj, result.result_hash)
    if workload in DETERMINISTIC and not hash_ok:
        return Outcome(FAILED, "HASH", reason="result_hash does not match result payload", penalty=rep.FAIL)

    if obj.get("status") != "COMPLETED":
        return Outcome(FAILED, "DEVICE_REPORTED", reason=f"device reported {obj.get('status')}: {obj.get('error', '')}"[:500],
                       penalty=rep.SOFT_FAIL, device_failed=True)

    if role_of(job) == CANARY:
        out = check_canary(spec, obj)
        if out.status == FAILED:
            return out
    if workload in DETERMINISTIC:
        out = check_deterministic(workload, job_payload(job), obj)
        out.evidence["hash_ok"] = hash_ok
        return out

    ok, why = check_schema(workload, obj)
    if not ok:
        return Outcome(FAILED, "SCHEMA", reason=why, penalty=rep.FAIL)
    return Outcome(PASSED, "SCHEMA", confidence="MEDIUM", reason=why, evidence={"hash_ok": hash_ok})


# ── DB-backed pieces ─────────────────────────────────────────────────────────

async def _eligible_other_device(db: AsyncSession, workload: str, exclude: set[str],
                                 prefer_not_provider: uuid.UUID | None, now: datetime) -> Optional[Device]:
    from app.services import benchmark_service as bench_svc
    from app.services.device_safety import SCHEDULABLE_STATUSES, dispatch_block_reason

    fresh = now - timedelta(minutes=10)
    rows = (
        await db.execute(
            select(Device).where(Device.status.in_(list(SCHEDULABLE_STATUSES)),
                                 Device.last_seen_at.is_not(None), Device.last_seen_at >= fresh)
            .order_by(Device.reputation_score.desc(), Device.farm_score.desc().nulls_last()).limit(50)
        )
    ).scalars().all()
    candidates = [d for d in rows if str(d.id) not in exclude]
    candidates.sort(key=lambda d: d.provider_id == prefer_not_provider)  # other providers first
    for d in candidates:
        t = await bench_svc.thresholds_for_device(db, d)
        if dispatch_block_reason(d, workload, t, now) is None:
            return d
    return None


async def create_shadow(db: AsyncSession, primary: Job, primary_device: Device, now: datetime) -> Optional[Job]:
    exclude = {str(primary_device.id), *[str(x) for x in (primary.verification_spec or {}).get("excluded_devices", [])]}
    device = await _eligible_other_device(db, primary.workload_type, exclude, primary_device.provider_id, now)
    if device is None:
        return None
    shadow = Job(
        workload_type=primary.workload_type,
        status=JobStatus.DISPATCHED,
        priority=max(1, int(primary.priority or 100) - 50),
        requested_resources=job_payload(primary),
        max_runtime_seconds=primary.max_runtime_seconds,
        adapter_id="internal-verification",
        customer_id=primary.customer_id,
        parent_job_id=primary.id,
        max_retries=0,
        retry_count=0,
        verification_spec={"role": REDUNDANT, "primary_device_id": str(primary_device.id)},
    )
    db.add(shadow)
    await db.flush()
    leases.add_event(db, shadow.id, None, JobStatus.CREATED.value, reason=f"redundant sample of {primary.id}")
    leases.add_event(db, shadow.id, JobStatus.CREATED.value, JobStatus.DISPATCHED.value,
                     reason=f"redundant execution on {device.human_id}")
    db.add(JobAssignment(job_id=shadow.id, device_id=device.id, assigned_at=now))
    await leases.issue_lease(db, shadow, device.id, now)
    logger.info("redundant_shadow_dispatched", primary_job_id=str(primary.id), shadow_job_id=str(shadow.id),
                device_id=str(device.id))
    return shadow


async def evaluate(db: AsyncSession, job: Job, result: JobResult, device: Optional[Device], now: datetime) -> Outcome:
    out = evaluate_static(job, result)
    if out.status != PASSED:
        return out
    role = role_of(job)
    if (job.workload_type in OBSERVATIONAL and role == PRIMARY and device is not None
            and sampled_for_redundancy(job.id, settings.REDUNDANT_SAMPLE_RATE)):
        shadow = await create_shadow(db, job, device, now)
        if shadow is not None:
            return Outcome(PENDING, "SCHEMA+REDUNDANT_SAMPLE", confidence="PENDING",
                           reason="awaiting redundant execution", evidence={"shadow_job_id": str(shadow.id)})
        if not settings.REDUNDANT_FALLBACK_SCHEMA:
            return Outcome(PENDING, "SCHEMA+REDUNDANT_SAMPLE", confidence="PENDING",
                           reason="no independent device available; awaiting manual review")
        out.confidence = "LOW"
        out.reason = "schema ok; no independent device for redundant sample"
        out.method = "SCHEMA(REDUNDANT_UNAVAILABLE)"
    return out


async def _load_job(db: AsyncSession, job_id: uuid.UUID) -> Optional[Job]:
    return (
        await db.execute(
            select(Job).options(selectinload(Job.assignment), selectinload(Job.result), selectinload(Job.verification))
            .where(Job.id == job_id)
        )
    ).scalar_one_or_none()


async def finalize(db: AsyncSession, job: Job, result: JobResult, outcome: Outcome, *,
                   actor_id: uuid.UUID | None = None, auto_credit_test: bool = True,
                   now: datetime | None = None) -> dict[str, Any]:
    """Apply a verification outcome. Caller commits."""
    from app.services.job_executor import credit_device_earnings

    now = now or datetime.now(timezone.utc)
    role = role_of(job)
    device = (await db.execute(select(Device).where(Device.id == result.device_id))).scalar_one_or_none()
    evidence = {
        "method": outcome.method, "confidence": outcome.confidence, "reason": outcome.reason,
        "result_hash": result.result_hash, "role": role, "policy": VERIFICATION_POLICY.get(job.workload_type, []),
        "attempt": int(job.retry_count or 0) + 1, **outcome.evidence,
    }
    verification = job.verification
    if verification is None:
        verification = JobVerification(job_id=job.id, status=outcome.status, method=outcome.method, evidence=evidence)
        db.add(verification)
        job.verification = verification
    else:
        verification.status, verification.method, verification.evidence = outcome.status, outcome.method, evidence
    verification.verified_at = now if outcome.status != PENDING else None

    if job.status == JobStatus.RESULT_SUBMITTED:
        leases.add_event(db, job.id, JobStatus.RESULT_SUBMITTED.value, JobStatus.VERIFYING.value,
                         reason=f"verification started ({outcome.method})", actor_id=actor_id)
        job.status = JobStatus.VERIFYING

    lease = await leases.latest_lease(db, job.id, result.device_id)
    response: dict[str, Any] = {"verification_status": outcome.status, "method": outcome.method,
                                "confidence": outcome.confidence, "reason": outcome.reason,
                                "test_earnings_credited": False, "reputation": None}

    if outcome.status == PENDING:
        job.updated_at = now
        response["job_status"] = job.status.value
        return response

    if outcome.status == PASSED:
        leases.add_event(db, job.id, JobStatus.VERIFYING.value, JobStatus.VERIFIED.value,
                         reason=f"{outcome.method}: {outcome.reason}"[:2000], actor_id=actor_id)
        job.status = JobStatus.VERIFIED
        if lease is not None:
            lease.outcome = "VERIFIED"
        if device is not None:
            response["reputation"] = await rep.record_outcome(db, device, rep.PASS, job_id=job.id, now=now)
        # Credit ONLY after verification, only for customer (PRIMARY) work.
        if (auto_credit_test and role == PRIMARY and job.workload_type != JobType.BENCHMARK.value):
            await credit_device_earnings(db, job, result.device_id, result, is_real_revenue=False)
            response["test_earnings_credited"] = True
    else:
        to = JobStatus.FAILED if outcome.device_failed else JobStatus.INVALID_RESULT
        leases.add_event(db, job.id, JobStatus.VERIFYING.value, to.value,
                         reason=f"{outcome.method}: {outcome.reason}"[:2000], actor_id=actor_id)
        job.status = to
        if lease is not None:
            lease.outcome = "INVALID_RESULT" if to == JobStatus.INVALID_RESULT else "DEVICE_FAILED"
            lease.evidence = {**(lease.evidence or {}), "verification": evidence}
        if device is not None and outcome.penalty:
            response["reputation"] = await rep.record_outcome(
                db, device, outcome.penalty, job_id=job.id, now=now,
                detail=f"job {job.id} ({job.workload_type}) {outcome.method}: {outcome.reason}")
        if role == PRIMARY and not outcome.evidence.get("job_invalid"):
            await leases.requeue_or_dead_letter(db, job, to, outcome.reason, now, failed_device_id=result.device_id)
        elif outcome.evidence.get("job_invalid"):
            job.dead_lettered = True
            job.last_error = outcome.reason
        if role == REDUNDANT and job.parent_job_id:
            await resolve_orphaned_primary(db, job.parent_job_id, now)

    job.updated_at = now
    response["job_status"] = job.status.value

    # A passing shadow resolves its primary by comparing observations.
    if outcome.status == PASSED and role == REDUNDANT and job.parent_job_id:
        response["primary"] = await _resolve_primary_with_shadow(db, job, result, now)
    return response


async def _resolve_primary_with_shadow(db: AsyncSession, shadow: Job, shadow_result: JobResult, now: datetime) -> Optional[dict]:
    primary = await _load_job(db, shadow.parent_job_id)
    if primary is None or primary.status != JobStatus.VERIFYING or primary.result is None:
        return None
    p_obj, s_obj = parse_result(primary.result) or {}, parse_result(shadow_result) or {}
    agree, why = compare_redundant(primary.workload_type, p_obj, s_obj)
    ev = {"shadow_job_id": str(shadow.id), "shadow_device_id": str(shadow_result.device_id)}
    if agree:
        out = Outcome(PASSED, "SCHEMA+REDUNDANT_SAMPLE", confidence="HIGH", reason=why, evidence=ev)
    else:
        # Two-way disagreement is inconclusive about who is wrong: soft penalty
        # to both, primary retried elsewhere.
        out = Outcome(FAILED, "SCHEMA+REDUNDANT_SAMPLE", confidence="MEDIUM", reason=f"redundant mismatch: {why}",
                      penalty=rep.SOFT_FAIL, evidence=ev)
        shadow_device = (await db.execute(select(Device).where(Device.id == shadow_result.device_id))).scalar_one_or_none()
        if shadow_device is not None:
            await rep.record_outcome(db, shadow_device, rep.SOFT_FAIL, job_id=shadow.id, now=now,
                                     detail=f"redundant disagreement with primary {primary.id}")
        if shadow.verification is not None:
            shadow.verification.evidence = {**(shadow.verification.evidence or {}), "disagreed_with_primary": True}
    return await finalize(db, primary, primary.result, out, now=now)


async def resolve_orphaned_primary(db: AsyncSession, primary_id: uuid.UUID, now: datetime) -> Optional[dict]:
    """Shadow could not produce a usable observation: fall back to schema-only."""
    primary = await _load_job(db, primary_id)
    if primary is None or primary.status != JobStatus.VERIFYING or primary.result is None:
        return None
    if not settings.REDUNDANT_FALLBACK_SCHEMA:
        return None
    out = evaluate_static(primary, primary.result)
    if out.status == PASSED:
        out.confidence = "LOW"
        out.method = "SCHEMA(REDUNDANT_UNAVAILABLE)"
        out.reason = "redundant execution failed to complete; schema-only"
    return await finalize(db, primary, primary.result, out, now=now)


async def verify_and_finalize(db: AsyncSession, job: Job, result: JobResult, *, actor_id: uuid.UUID | None = None,
                              auto_credit_test: bool = True, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    device = (await db.execute(select(Device).where(Device.id == result.device_id))).scalar_one_or_none()
    outcome = await evaluate(db, job, result, device, now)
    logger.info("job_verification", job_id=str(job.id), status=outcome.status, method=outcome.method,
                reason=outcome.reason)
    return await finalize(db, job, result, outcome, actor_id=actor_id, auto_credit_test=auto_credit_test, now=now)


# ── canaries (known-answer spot checks) ─────────────────────────────────────

def canary_payload_and_expected(workload: str, seed: str, url: str | None = None,
                                expected_http_status: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if workload == JobType.CHECKSUM.value:
        payload = {"seed": seed, "size_bytes": 256 * 1024, "algorithm": "sha256"}
        exp = cpu_data.run_checksum(payload)
        return payload, {"output_sha256": exp["output_sha256"], "size_bytes": exp["size_bytes"]}
    if workload == JobType.JSON_TRANSFORM.value:
        doc = [{"id": i, "group": "a" if i % 3 else "b", "v": (i * 7919) % 1000} for i in range(50)]
        payload = {"document": doc, "ops": [{"op": "filter_eq", "field": "group", "value": "a"},
                                            {"op": "sum", "field": "v"}], "nonce": seed}
        exp = cpu_data.run_json_transform(payload)
        return payload, {"output_sha256": exp["output_sha256"]}
    if workload == JobType.COMPRESSION.value:
        payload = {"seed": seed, "size_bytes": 64 * 1024}
        exp = cpu_data.run_compression(payload)
        return payload, {"input_sha256": exp["input_sha256"], "input_size": exp["input_size"]}
    if workload == JobType.HTTP_CHECK.value:
        if not url or expected_http_status is None:
            raise ValueError("http_check canary needs url and expected_http_status")
        return {"url": url, "method": "GET"}, {"http_status": int(expected_http_status)}
    raise ValueError(f"no canary for {workload}")
