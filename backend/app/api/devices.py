"""Device enrollment and node-auth endpoints for FARMOS."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.core import Provider
from app.db.models.devices import Device, DeviceCapability, DeviceHeartbeat
from app.db.models.enums import DeviceStatus, RoleName
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
    request_nonce: str
    request_timestamp: str
    signature: str


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
        verify_request_signature(
            device.public_key,
            req.model_dump(mode="json", exclude={"signature"}),
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
        event_metadata=network,
    )
    db.add(hb)

    new_status = device.status
    if device.status in {DeviceStatus.ENROLLING, DeviceStatus.BENCHMARKING, DeviceStatus.IDLE, DeviceStatus.OFFLINE}:
        new_status = DeviceStatus.ACTIVE

    await db.execute(
        update(Device)
        .where(Device.id == device.id)
        .values(
            last_seen_at=datetime.now(timezone.utc),
            enrolled_at=device.enrolled_at or datetime.now(timezone.utc),
            status=new_status,
        )
    )
    await db.commit()
    return {"detail": "heartbeat accepted", "device_id": device_id, "status": new_status.value}


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
    return {
        "device_id": str(device.id),
        "human_id": device.human_id,
        "status": device.status.value,
        "farm_score": device.farm_score,
        "reputation_score": float(device.reputation_score) if device.reputation_score is not None else None,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
    }