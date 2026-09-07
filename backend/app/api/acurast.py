"""Acurast Processor Management Backend-compatible FARMOS endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.acurast import AcurastProcessor
from app.db.models.devices import Device, DeviceHeartbeat
from app.db.models.enums import DeviceStatus, RoleName
from app.db.session import get_db
from app.dependencies import require_roles
from app.security.acurast_processor import verify_acurast_processor_signature

admin_router = APIRouter()
management_router = APIRouter()


class ProcessorRegistrationRequest(BaseModel):
    address: str = Field(min_length=10, max_length=100)
    label: str | None = Field(default=None, max_length=100)
    farmos_device_id: UUID | None = None


class AcurastCheckIn(BaseModel):
    """Official Acurast Android management check-in payload."""

    model_config = ConfigDict(extra="forbid")

    deviceAddress: str = Field(min_length=10, max_length=100)
    platform: Literal[0]
    timestamp: int = Field(ge=0)
    batteryLevel: float = Field(ge=0, le=100)
    isCharging: bool
    batteryHealth: str | None = Field(default=None, max_length=50)
    temperatures: dict[str, float | None] | None = None
    networkType: Literal["wifi", "cellular", "usb", "unknown"]
    ssid: str | None = Field(default=None, max_length=255)


async def _get_processor_or_404(address: str, db: AsyncSession) -> AcurastProcessor:
    result = await db.execute(
        select(AcurastProcessor).where(AcurastProcessor.address == address)
    )
    processor = result.scalar_one_or_none()
    if not processor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Processor not found")
    return processor


def _status_payload(processor: AcurastProcessor) -> dict:
    if processor.last_reported_at is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Processor has not checked in")
    return {
        "address": processor.address,
        "timestamp": int(processor.last_reported_at.timestamp() * 1000),
        "batteryLevel": processor.battery_level,
        "isCharging": processor.is_charging,
        "batteryHealth": processor.battery_health,
        "networkType": processor.network_type,
        "ssid": processor.ssid,
    }


@admin_router.post("/processors", status_code=status.HTTP_201_CREATED)
async def register_processor(
    payload: ProcessorRegistrationRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles(RoleName.OWNER, RoleName.ADMIN)),
):
    address = payload.address.strip()
    existing_result = await db.execute(
        select(AcurastProcessor).where(AcurastProcessor.address == address)
    )
    if existing_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Processor already registered")

    if payload.farmos_device_id:
        device_result = await db.execute(select(Device).where(Device.id == payload.farmos_device_id))
        if not device_result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="FARMOS device not found")

    processor = AcurastProcessor(
        address=address,
        label=payload.label.strip() if payload.label else None,
        farmos_device_id=payload.farmos_device_id,
        enabled=True,
    )
    db.add(processor)
    await db.commit()
    await db.refresh(processor)
    return {
        "processor_id": str(processor.id),
        "address": processor.address,
        "enabled": processor.enabled,
        "farmos_device_id": str(processor.farmos_device_id) if processor.farmos_device_id else None,
    }


@management_router.post("/processor/check-in")
async def check_in(
    payload: AcurastCheckIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Accept a signature-verified check-in from a pre-registered Android processor."""
    signature_hex = request.headers.get("X-Device-Signature")
    if not signature_hex:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device signature required")

    result = await db.execute(
        select(AcurastProcessor).where(AcurastProcessor.address == payload.deviceAddress)
    )
    processor = result.scalar_one_or_none()
    if not processor or not processor.enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Processor is not registered")

    # The official backend verifies ``JSON.stringify(request)``. Recreate that
    # compact representation from the parsed wire object so `82` remains `82`
    # (rather than Pydantic-normalized `82.0`), while transport whitespace does
    # not influence verification.
    parsed_body = json.loads(await request.body())
    signed_body = json.dumps(parsed_body, separators=(",", ":")).encode()
    if not verify_acurast_processor_signature(payload.deviceAddress, signed_body, signature_hex):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Acurast processor signature",
        )

    observed_at = datetime.fromtimestamp(payload.timestamp / 1000, tz=timezone.utc)
    processor.platform = payload.platform
    processor.last_reported_at = observed_at
    processor.battery_level = payload.batteryLevel
    processor.is_charging = payload.isCharging
    processor.battery_health = payload.batteryHealth
    processor.temperature_c = (payload.temperatures or {}).get("battery")
    processor.network_type = payload.networkType
    processor.ssid = payload.ssid
    processor.latest_check_in = payload.model_dump(exclude_none=True)

    if processor.farmos_device_id:
        device_result = await db.execute(select(Device).where(Device.id == processor.farmos_device_id))
        device = device_result.scalar_one_or_none()
        if device:
            device.last_seen_at = datetime.now(timezone.utc)
            if device.status in {
                DeviceStatus.ENROLLING,
                DeviceStatus.BENCHMARKING,
                DeviceStatus.IDLE,
                DeviceStatus.OFFLINE,
            }:
                device.status = DeviceStatus.ACTIVE
            db.add(
                DeviceHeartbeat(
                    device_id=device.id,
                    observed_at=observed_at,
                    battery_pct=payload.batteryLevel,
                    charging=payload.isCharging,
                    temperature_c=processor.temperature_c,
                    network_type=payload.networkType,
                    event_metadata={
                        "source": "acurast",
                        "processor_address": payload.deviceAddress,
                        "ssid": payload.ssid,
                    },
                )
            )

    await db.commit()
    return {
        "success": True,
        "refreshIntervalInSeconds": settings.ACURAST_CHECKIN_REFRESH_SECONDS,
    }


@management_router.get("/processor/api/{address}/status")
async def processor_status(address: str, db: AsyncSession = Depends(get_db)):
    processor = await _get_processor_or_404(address, db)
    return {"processorStatus": _status_payload(processor)}


@management_router.get("/processor/api/status")
async def all_processor_statuses(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AcurastProcessor)
        .where(AcurastProcessor.enabled.is_(True), AcurastProcessor.last_reported_at.is_not(None))
        .order_by(AcurastProcessor.last_reported_at.desc())
    )
    return {"processorStatuses": [_status_payload(processor) for processor in result.scalars()]}
