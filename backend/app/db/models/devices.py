from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.enums import DeviceStatus
from app.db.session import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    farm_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("farms.id", ondelete="SET NULL"), nullable=True
    )
    human_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    public_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hardware_fingerprint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    android_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    architecture: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    cpu_cores: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ram_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    storage_total_mb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, native_enum=False), default=DeviceStatus.ENROLLING, nullable=False
    )
    farm_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    reputation_score: Mapped[float] = mapped_column(Numeric(8, 4), default=50.0, nullable=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enrolled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    provider: Mapped["Provider"] = relationship("Provider", back_populates="devices")
    farm: Mapped[Optional["Farm"]] = relationship("Farm")
    heartbeats: Mapped[list["DeviceHeartbeat"]] = relationship(
        "DeviceHeartbeat", back_populates="device", cascade="all, delete-orphan"
    )
    benchmarks: Mapped[list["DeviceBenchmark"]] = relationship(
        "DeviceBenchmark", back_populates="device", cascade="all, delete-orphan"
    )
    capabilities: Mapped[list["DeviceCapability"]] = relationship(
        "DeviceCapability", back_populates="device", cascade="all, delete-orphan"
    )
    health_events: Mapped[list["DeviceHealthEvent"]] = relationship(
        "DeviceHealthEvent", back_populates="device", cascade="all, delete-orphan"
    )
    job_assignments: Mapped[list["JobAssignment"]] = relationship(
        "JobAssignment", back_populates="device"
    )

    __table_args__ = (
        Index("ix_devices_provider_id", "provider_id"),
        Index("ix_devices_farm_id", "farm_id"),
        Index("ix_devices_status", "status"),
        Index("ix_devices_last_seen", "last_seen_at"),
    )


class DeviceHeartbeat(Base):
    __tablename__ = "device_heartbeats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    battery_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    charging: Mapped[Optional[bool]] = mapped_column(nullable=True)
    temperature_c: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    cpu_util_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    ram_used_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    storage_free_mb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    network_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    down_mbps: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    up_mbps: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    app_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    event_metadata: Mapped[Any] = mapped_column("metadata", JSON, default=dict, nullable=False)

    device: Mapped["Device"] = relationship("Device", back_populates="heartbeats")

    __table_args__ = (
        Index("ix_heartbeats_device_time", "device_id", desc("observed_at")),
        Index("ix_heartbeats_observed_at", "observed_at"),
    )


class DeviceBenchmark(Base):
    __tablename__ = "device_benchmarks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    suite_version: Mapped[str] = mapped_column(String(50), nullable=False)
    cpu_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    ai_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    memory_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    storage_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    network_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    thermal_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    power_efficiency_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    reliability_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    farm_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    raw_results: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    device: Mapped["Device"] = relationship("Device", back_populates="benchmarks")

    __table_args__ = (
        Index("ix_benchmarks_device_id", "device_id"),
        Index("ix_benchmarks_completed_at", "completed_at"),
    )


class DeviceCapability(Base):
    __tablename__ = "device_capabilities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    device: Mapped["Device"] = relationship("Device", back_populates="capabilities")

    __table_args__ = (
        UniqueConstraint("device_id", "capability", name="uq_device_capability"),
    )


class DeviceHealthEvent(Base):
    __tablename__ = "device_health_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)  # INFO, WARNING, CRITICAL
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[Any] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    device: Mapped["Device"] = relationship("Device", back_populates="health_events")

    __table_args__ = (
        Index("ix_health_events_device_id", "device_id"),
        Index("ix_health_events_created_at", "created_at"),
        Index("ix_health_events_severity", "severity"),
    )
