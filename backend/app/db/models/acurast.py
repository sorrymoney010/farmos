"""Acurast processor records managed by the FARMOS control plane."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, JSON, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

if TYPE_CHECKING:
    from app.db.models.devices import Device


class AcurastProcessor(Base):
    """A pre-registered Acurast Processor permitted to use FARMOS as manager."""

    __tablename__ = "acurast_processors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    address: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    farmos_device_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    platform: Mapped[Optional[int]] = mapped_column(nullable=True)
    last_reported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    battery_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_charging: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    battery_health: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    temperature_c: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ssid: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    latest_check_in: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    farmos_device: Mapped[Optional["Device"]] = relationship("Device")

    __table_args__ = (
        UniqueConstraint("address", name="uq_acurast_processors_address"),
        Index("ix_acurast_processors_farmos_device_id", "farmos_device_id"),
        Index("ix_acurast_processors_enabled", "enabled"),
        Index("ix_acurast_processors_last_reported_at", "last_reported_at"),
    )
