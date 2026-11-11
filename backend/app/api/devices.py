"""Device enrollment and node-auth endpoints for FARMOS."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.core import Provider
from app.db.models.devices import Device, DeviceCapability, DeviceHeartbeat
from app.db.models.enums import (
    DeviceStatus,
    JobEventActorType,
    JobStatus,
    JobType,
    RoleName,
)
from app.db.models.jobs_ledger import Job, JobAssignment, JobEvent, JobVerification
from app.services import benchmark_service as bench_svc
from app.services.device_safety import dispatch_block_reason
from app.db.session import get_db
from app.dependencies import get_current_user, require_roles
from app.logging import get_logger
from app.security.node_auth import (
    consume_enrollment_token,
    ensure_timestamp_fresh,
    exchange_claim_code,
    issue_claim_code,
    issue_enrollment_token,
    issue_node_token,
    mark_nonce_used,
    verify_node_token,
    verify_request_signature,
)

logger = get_logger(__name__)
router = APIRouter()
node_bearer = HTTPBearer(auto_error=False)


class EnrollmentTokenRequest(BaseModel):
    farm_id: str | None = None
    expires_in_minutes: int = Field(default=settings.DEVICE_ENROLLMENT_TOKEN_EXPIRE_MINUTES, ge=1, le=60)


class ClaimCodeRequest(BaseModel):
    farm_id: str | None = None
    expires_in_minutes: int = Field(default=settings.CLAIM_CODE_EXPIRE_MINUTES, ge=1, le=60)


class ClaimCodeExchangeRequest(BaseModel):
    claim_code: str = Field(min_length=1, max_length=32)


class EnrollRequest(BaseModel):
    enrollment_token: str
    device_public_key: str
    hardware_fingerprint: str
    profile: dict[str, Any]
    request_nonce: str
    request_timestamp: str
    signature: str


class HeartbeatRequest(BaseModel):
    observed_at: str
    battery_pct: float | None = None
    charging: bool | None = None
    temperature_c: float | None = None
    cpu_util_pct: float | None = None
    ram_used_mb: int | None = None
    storage_free_mb: int | None = None
    network: dict[str, Any] | None = None
    app_version: str | None = None
    # Sprint 3 safety telemetry (all optional / backward compatible).
    battery_health: str | None = Field(default=None, max_length=50)
    battery_temperature_c: float | None = None
    thermal_status: int | None = Field(default=None, ge=0, le=6)
    request_nonce: str
    request_timestamp: str
    signature: str


class BenchmarkResultRequest(BaseModel):
    """Signed benchmark result (suite farmos-bench-1).

    ``raw_results_json`` is the exact JSON string the device produced; the device
    signs its SHA-256 (``results_sha256``) so float formatting can never break
    signature verification between Kotlin and Python.
    Signed canonical payload: {request_nonce, request_timestamp, device_id,
    suite_version, job_id ("" if none), results_sha256}.
    """

    suite_version: str = Field(min_length=1, max_length=50)
    job_id: str | None = None
    raw_results_json: str = Field(min_length=2, max_length=200_000)
    results_sha256: str = Field(min_length=64, max_length=64)
    request_nonce: str
    request_timestamp: str
    signature: str


class BenchmarkRequest(BaseModel):
    suite_version: str | None = None


@router.post("/enrollment-token")
async def create_enrollment_token(
    payload: EnrollmentTokenRequest,
    current_user=Depends(require_roles(RoleName.OWNER, RoleName.ADMIN, RoleName.PROVIDER)),
):
    token, expires_at = await issue_enrollment_token(
        str(current_user.id),
        farm_id=payload.farm_id,
        expires_in_minutes=payload.expires_in_minutes,
    )
    return {
        "enrollment_token": token,
        "expires_at": expires_at.isoformat(),
        "qr_payload": f"farmos://enroll?token={token}",
    }


@router.post("/staging/enrollment-token")
async def staging_enrollment_token(
    payload: EnrollmentTokenRequest,
    request: Request,
):
    admin_secret = request.headers.get("X-Staging-Enroll-Admin", "")
    if not admin_secret or not secrets.compare_digest(admin_secret, settings.STAGING_ENROLL_ADMIN_SECRET):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid staging admin secret")
    owner_user_id = settings.STAGING_ADMIN_USER_ID or "29443001-3ae8-40e3-91f8-969d97eda184"
    token, expires_at = await issue_enrollment_token(
        owner_user_id,
        farm_id=payload.farm_id,
        expires_in_minutes=payload.expires_in_minutes,
    )
    return {
        "enrollment_token": token,
        "expires_at": expires_at.isoformat(),
        "qr_payload": f"{settings.PUBLIC_API_BASE_URL or str(request.base_url).rstrip('/')}/enroll?token={token}",
    }



@router.post("/claim-code")
async def create_claim_code(
    payload: ClaimCodeRequest,
    current_user=Depends(require_roles(RoleName.OWNER, RoleName.ADMIN, RoleName.PROVIDER)),
):
    """Mint a short claim/pairing code for wireless (no-USB) device onboarding."""
    code, expires_at = await issue_claim_code(
        str(current_user.id),
        farm_id=payload.farm_id,
        expires_in_minutes=payload.expires_in_minutes,
    )
    return {
        "claim_code": code,
        "expires_at": expires_at.isoformat(),
        "instructions": "Enter this code on the device (FARMOS Node app or agent). No USB required.",
    }


@router.post("/staging/claim-code")
async def staging_claim_code(
    payload: ClaimCodeRequest,
    request: Request,
):
    """Staging mint of a short claim code (X-Staging-Enroll-Admin)."""
    admin_secret = request.headers.get("X-Staging-Enroll-Admin", "")
    if not admin_secret or not secrets.compare_digest(admin_secret, settings.STAGING_ENROLL_ADMIN_SECRET):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid staging admin secret")
    owner_user_id = settings.STAGING_ADMIN_USER_ID or "29443001-3ae8-40e3-91f8-969d97eda184"
    code, expires_at = await issue_claim_code(
        owner_user_id,
        farm_id=payload.farm_id,
        expires_in_minutes=payload.expires_in_minutes,
    )
    return {
        "claim_code": code,
        "expires_at": expires_at.isoformat(),
        "staging_fixed_code": "123" if settings.STAGING_CLAIM_CODES else None,
        "instructions": (
            "Enter claim_code on the device. When STAGING_CLAIM_CODES=true, code 123 also works."
        ),
    }


@router.post("/claim-code/exchange")
async def exchange_claim_code_endpoint(payload: ClaimCodeExchangeRequest):
    """Exchange a short claim code for a one-time enrollment token (network path, no USB).

    The device then calls POST /enroll with the enrollment token and a device signature.
    Signature verification still happens before the enrollment token is consumed.
    """
    try:
        token, expires_at = await exchange_claim_code(payload.claim_code)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {
        "enrollment_token": token,
        "expires_at": expires_at.isoformat(),
        "qr_payload": f"farmos://enroll?token={token}",
    }


@router.post("/enroll")
async def enroll_device(
    req: EnrollRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        ensure_timestamp_fresh(req.request_timestamp, max_skew_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    nonce_ok = await mark_nonce_used("enroll", req.request_nonce, ttl_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    if not nonce_ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Replay detected")

    # Verify the device signature BEFORE consuming the one-time token, so an invalid
    # signed request cannot burn a valid enrollment token.
    try:
        verify_request_signature(
            req.device_public_key,
            req.model_dump(mode="json", exclude={"signature"}),
            req.signature,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    token_data = await consume_enrollment_token(req.enrollment_token)
    if not token_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired enrollment token")

    provider_result = await db.execute(
        select(Provider).where(Provider.user_id == token_data["owner_user_id"])
    )
    provider = provider_result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No provider record for token owner")

    existing = await db.execute(select(Device).where(Device.public_key == req.device_public_key))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device public key already enrolled")

    human_id = f"FARM-NODE-{datetime.now(timezone.utc).strftime('%H%M%S')}-{req.request_nonce[:6].upper()}"
    device = Device(
        provider_id=provider.id,
        farm_id=token_data.get("farm_id"),
        human_id=human_id,
        public_key=req.device_public_key,
        hardware_fingerprint=req.hardware_fingerprint,
        manufacturer=req.profile.get("manufacturer"),
        model=req.profile.get("model"),
        android_version=req.profile.get("android_version"),
        architecture=req.profile.get("architecture"),
        cpu_cores=req.profile.get("cpu_cores"),
        ram_mb=req.profile.get("ram_mb"),
        storage_total_mb=req.profile.get("storage_total_mb"),
        status=DeviceStatus.BENCHMARKING,
        enrolled_at=datetime.now(timezone.utc),
    )
    db.add(device)
    await db.flush()

    for cap in req.profile.get("capabilities", ["cpu_compute", "ai_inference", "network", "storage"]):
        db.add(DeviceCapability(device_id=device.id, capability=cap, enabled=True))

    await db.commit()
    logger.info("device_enrolled", device_id=str(device.id), human_id=device.human_id)
    return {
        "device_id": str(device.id),
        "human_id": device.human_id,
        "node_token": issue_node_token(str(device.id)),
        "status": DeviceStatus.BENCHMARKING.value,
        "heartbeat_interval_seconds": 30,
    }


@router.post("/{device_id}/heartbeat")
async def post_heartbeat(
    device_id: str,
    req: HeartbeatRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
    db: AsyncSession = Depends(get_db),
):
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token required")

    token_payload = verify_node_token(credentials.credentials)
    if not token_payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token")
    if token_payload.get("sub") != device_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node token/device mismatch")

    try:
        ensure_timestamp_fresh(req.request_timestamp, max_skew_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    nonce_ok = await mark_nonce_used(f"heartbeat:{device_id}", req.request_nonce, ttl_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    if not nonce_ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Replay detected")

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    try:
        # Sign-what-you-send: verify over the fields the device actually sent
        # (exclude_unset), so newer optional telemetry fields never break older
        # clients' signatures and nothing unsigned is ever trusted.
        verify_request_signature(
            device.public_key,
            req.model_dump(mode="json", exclude={"signature"}, exclude_unset=True),
            req.signature,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    network = req.network or {}
    observed_at = datetime.fromisoformat(req.observed_at.replace("Z", "+00:00"))
    hb = DeviceHeartbeat(
        device_id=device.id,
        observed_at=observed_at,
        battery_pct=req.battery_pct,
        charging=req.charging,
        temperature_c=req.temperature_c,
        cpu_util_pct=req.cpu_util_pct,
        ram_used_mb=req.ram_used_mb,
        storage_free_mb=req.storage_free_mb,
        network_type=network.get("type"),
        down_mbps=network.get("down_mbps"),
        up_mbps=network.get("up_mbps"),
        app_version=req.app_version,
        event_metadata={
            **network,
            "battery_health": req.battery_health,
            "battery_temperature_c": req.battery_temperature_c,
            "thermal_status": req.thermal_status,
        },
    )
    db.add(hb)

    # Lifecycle: onboarding/offline nodes come online. BENCHMARKING only leaves
    # via a passing benchmark when BENCHMARK_REQUIRED_FOR_ACTIVE (spec §8).
    promotable = {DeviceStatus.ENROLLING, DeviceStatus.IDLE, DeviceStatus.OFFLINE}
    if not settings.BENCHMARK_REQUIRED_FOR_ACTIVE:
        promotable.add(DeviceStatus.BENCHMARKING)
    if device.status in promotable:
        device.status = (
            DeviceStatus.BENCHMARKING
            if device.status == DeviceStatus.ENROLLING and settings.BENCHMARK_REQUIRED_FOR_ACTIVE
            else DeviceStatus.ACTIVE
        )

    # Thermal / battery governor — protect hardware (spec §10).
    decision = await bench_svc.apply_heartbeat_safety(
        db,
        device,
        temperature_c=req.temperature_c,
        battery_pct=req.battery_pct,
        charging=req.charging,
        battery_health=req.battery_health,
        battery_temperature_c=req.battery_temperature_c,
        thermal_status=req.thermal_status,
    )
    device.last_seen_at = datetime.now(timezone.utc)
    device.enrolled_at = device.enrolled_at or datetime.now(timezone.utc)
    new_status = device.status
    benchmark_required = device.status == DeviceStatus.BENCHMARKING
    farm_score = float(device.farm_score) if device.farm_score is not None else None
    await db.commit()
    return {
        "detail": "heartbeat accepted",
        "device_id": device_id,
        "status": new_status.value,
        "farm_score": farm_score,
        "benchmark_required": benchmark_required,
        "benchmark_suite_version": settings.BENCHMARK_SUITE_VERSION,
        "safety": decision.to_response(),
    }


async def _authenticated_device(
    device_id: str,
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> Device:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token required")
    token_payload = verify_node_token(credentials.credentials)
    if not token_payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token")
    if token_payload.get("sub") != device_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node token/device mismatch")
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


@router.post("/{device_id}/benchmark/result")
async def post_benchmark_result(
    device_id: str,
    req: BenchmarkResultRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(node_bearer),
    db: AsyncSession = Depends(get_db),
):
    """Device submits a signed benchmark result; server scores it and updates FarmScore."""
    device = await _authenticated_device(device_id, credentials, db)
    try:
        ensure_timestamp_fresh(req.request_timestamp, max_skew_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    signed = {
        "request_nonce": req.request_nonce,
        "request_timestamp": req.request_timestamp,
        "device_id": device_id,
        "suite_version": req.suite_version,
        "job_id": req.job_id or "",
        "results_sha256": req.results_sha256,
    }
    try:
        verify_request_signature(device.public_key, signed, req.signature)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    if not secrets.compare_digest(bench_svc.results_sha256(req.raw_results_json), req.results_sha256.lower()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="results_sha256 mismatch")
    nonce_ok = await mark_nonce_used(
        f"benchmark:{device_id}", req.request_nonce, ttl_seconds=settings.NODE_REQUEST_MAX_SKEW_SECONDS
    )
    if not nonce_ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Replay detected")

    if device.status in {DeviceStatus.QUARANTINED, DeviceStatus.SUSPENDED, DeviceStatus.RETIRED}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Device is {device.status.value}")
    try:
        raw = bench_svc.parse_raw_results(req.raw_results_json)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid raw results: {exc}")

    job: Job | None = None
    if req.job_id:
        try:
            job_uuid = UUID(req.job_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id")
        job = (
            await db.execute(
                select(Job).join(JobAssignment).where(
                    Job.id == job_uuid,
                    JobAssignment.device_id == device.id,
                    Job.workload_type == JobType.BENCHMARK.value,
                    Job.status.in_([JobStatus.DISPATCHED, JobStatus.ACCEPTED, JobStatus.RUNNING]),
                )
            )
        ).scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark job not found for device")

    benchmark, passed, reasons = await bench_svc.record_result(
        db, device, req.suite_version, raw, job_id=job.id if job else None
    )

    if job is not None:
        now = datetime.now(timezone.utc)
        prev = job.status
        # Benchmarks are internal: VERIFIED as recorded, never revenue/ledger credit.
        for frm, to in ((prev, JobStatus.RESULT_SUBMITTED), (JobStatus.RESULT_SUBMITTED, JobStatus.VERIFIED)):
            db.add(JobEvent(job_id=job.id, from_status=frm.value, to_status=to.value,
                            actor_type=JobEventActorType.NODE if to == JobStatus.RESULT_SUBMITTED
                            else JobEventActorType.SYSTEM,
                            actor_id=device.id if to == JobStatus.RESULT_SUBMITTED else None,
                            reason="benchmark recorded"))
        job.status = JobStatus.VERIFIED
        job.updated_at = now
        db.add(JobVerification(
            job_id=job.id, status="PASSED" if passed else "FAILED", method="benchmark_recorded",
            evidence={"benchmark_id": str(benchmark.id), "results_sha256": req.results_sha256,
                      "farm_score": float(benchmark.farm_score), "baseline_failures": reasons},
            verified_at=now,
        ))
        if device.status == DeviceStatus.RUNNING:
            device.status = DeviceStatus.IDLE

    await db.commit()
    logger.info("benchmark_recorded", device_id=device_id, farm_score=float(benchmark.farm_score), passed=passed)
    return {
        **bench_svc.benchmark_to_dict(benchmark),
        "device_id": device_id,
        "status": device.status.value,
    }


@router.post("/{device_id}/benchmark")
async def request_benchmark(
    device_id: str,
    payload: BenchmarkRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles(RoleName.OWNER, RoleName.ADMIN, RoleName.OPERATIONS)),
):
    """Create + dispatch a benchmark job to a device (BenchmarkService.request_benchmark)."""
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    thresholds = await bench_svc.thresholds_for_device(db, device)
    blocked = dispatch_block_reason(device, JobType.BENCHMARK.value, thresholds, datetime.now(timezone.utc))
    if blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Benchmark blocked by safety policy: {blocked}")

    suite = (payload.suite_version if payload else None) or settings.BENCHMARK_SUITE_VERSION
    now = datetime.now(timezone.utc)
    job = Job(
        workload_type=JobType.BENCHMARK.value,
        status=JobStatus.DISPATCHED,
        priority=10,
        requested_resources={"suite_version": suite, "max_runtime_seconds": 120},
        adapter_id="internal",
        customer_id=current_user.id,
        max_runtime_seconds=120,
    )
    db.add(job)
    await db.flush()
    db.add(JobAssignment(job_id=job.id, device_id=device.id, assigned_at=now))
    db.add(JobEvent(job_id=job.id, from_status=None, to_status=JobStatus.CREATED.value,
                    actor_type=JobEventActorType.USER, actor_id=current_user.id))
    db.add(JobEvent(job_id=job.id, from_status=JobStatus.CREATED.value, to_status=JobStatus.DISPATCHED.value,
                    actor_type=JobEventActorType.SYSTEM, reason="benchmark requested"))
    await db.commit()
    return {"job_id": str(job.id), "device_id": device_id, "suite_version": suite,
            "status": JobStatus.DISPATCHED.value}


@router.get("/{device_id}/benchmarks")
async def list_benchmarks(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    from app.db.models.devices import DeviceBenchmark

    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    rows = (
        await db.execute(
            select(DeviceBenchmark)
            .where(DeviceBenchmark.device_id == device.id)
            .order_by(DeviceBenchmark.completed_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {"device_id": device_id, "farm_score": _f(device.farm_score),
            "benchmarks": [bench_svc.benchmark_to_dict(b) for b in rows]}


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


def device_health_dict(device: Device) -> dict[str, Any]:
    return {
        "temperature_state": device.temperature_state or "UNKNOWN",
        "temperature_c": _f(device.last_temperature_c),
        "battery_pct": _f(device.last_battery_pct),
        "charging": device.last_charging,
        "battery_health": device.battery_health,
        "safety_paused": bool(device.safety_paused),
        "safety_reason": device.safety_reason,
        "cooldown_until": device.cooldown_until.isoformat() if device.cooldown_until else None,
    }


@router.get("/{device_id}")
async def get_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    latest = await bench_svc.get_latest_scores(db, device.id)
    return {
        "device_id": str(device.id),
        "human_id": device.human_id,
        "status": device.status.value,
        "model": device.model,
        "farm_score": _f(device.farm_score),
        "reputation_score": float(device.reputation_score) if device.reputation_score is not None else None,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "health": device_health_dict(device),
        "latest_benchmark": bench_svc.benchmark_to_dict(latest) if latest else None,
    }
