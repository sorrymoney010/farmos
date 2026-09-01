"""Device endpoints for FARMOS."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.core import User, UserRole, Provider
from app.db.models.devices import Device, DeviceCapability
from app.db.models.enums import DeviceStatus, RoleName
from app.db.session import get_db
from app.dependencies import require_roles, get_current_user
from app.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


class EnrollRequest(BaseModel):
    enrollment_token: str
    device_public_key: str
    hardware_fingerprint: str
    profile: dict[str, Any]


class HeartbeatRequest(BaseModel):
    observed_at: datetime
    battery_pct: float | None = None
    charging: bool | None = None
    temperature_c: float | None = None
    cpu_util_pct: float | None = None
    ram_used_mb: int | None = None
    storage_free_mb: int | None = None
    network: dict[str, Any] | None = None
    app_version: str | None = None


@router.post("/enrollment-token")
async def create_enrollment_token(
    payload: dict[str, Any],
    current_user=Depends(get_current_user),
):
    token = f"enroll-{uuid4().hex}"
    return {
        "enrollment_token": token,
        "expires_at": datetime.now(timezone.utc).isoformat(),
        "qr_payload": f"farmos://enroll?token={token}",
    }


@router.post("/enroll")
async def enroll_device(
    req: EnrollRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if req.enrollment_token != f"enroll-{req.enrollment_token.split('-', 1)[-1]}":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid enrollment token")
    # Find provider for current user
    user_result = await db.execute(select(Provider).where(Provider.user_id == current_user.id))
    provider = user_result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No provider record for user")
    human_id = f"FARM-NODE-{uuid4().hex[:6].upper()}"
    device = Device(
        provider_id=provider.id,
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
        status=DeviceStatus.ENROLLING,
    )
    db.add(device)
    await db.flush()
    caps = ["cpu_compute", "ai_inference", "network", "storage"]
    for cap in caps:
        db.add(DeviceCapability(device_id=device.id, capability=cap, enabled=True))
    await db.commit()
    logger.info("device_enrolled", device_id=str(device.id), human_id=device.human_id)
    return {
        "device_id": str(device.id),
        "human_id": device.human_id,
        "node_token": f"node-token-{device.id.hex[:16]}",
        "status": DeviceStatus.BENCHMARKING.value,
        "heartbeat_interval_seconds": 30,
    }


@router.post("/{device_id}/heartbeat")
async def post_heartbeat(
    device_id: str,
    req: HeartbeatRequest,
    db: AsyncSession = Depends(get_db),
):
    from app.db.models.devices import DeviceHeartbeat  # noqa: PLC0415
    from sqlalchemy import update  # noqa: PLC0415
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    hb = DeviceHeartbeat(
        device_id=device.id,
        observed_at=req.observed_at,
        battery_pct=req.battery_pct,
        charging=req.charging,
        temperature_c=req.temperature_c,
        cpu_util_pct=req.cpu_util_pct,
        ram_used_mb=req.ram_used_mb,
        storage_free_mb=req.storage_free_mb,
        app_version=req.app_version,
        event_metadata=req.network or {},
    )
    db.add(hb)
    await db.execute(
        update(Device).where(Device.id == device.id).values(last_seen_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return {"detail": "heartbeat accepted"}


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
        "reputation_score": device.reputation_score,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
    }
