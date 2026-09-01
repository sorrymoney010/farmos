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
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.enums import (
    JobEventActorType,
    JobStatus,
    LedgerAccountType,
    LedgerDirection,
    OpportunityStatus,
    WithdrawalStatus,
)
from app.db.session import Base


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    adapter_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_symbol: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    estimated_revenue_usd_hour: Mapped[Optional[float]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    payment_confidence: Mapped[float] = mapped_column(
        Numeric(6, 5), default=0.5, nullable=False
    )
    risk_score: Mapped[float] = mapped_column(Numeric(6, 5), default=0.5, nullable=False)
    min_farm_score: Mapped[Optional[float]] = mapped_column(Numeric(8, 4), nullable=True)
    region_rules: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    resource_requirements: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    constraints: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    available_capacity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[OpportunityStatus] = mapped_column(
        Enum(OpportunityStatus, native_enum=False), default=OpportunityStatus.ACTIVE, nullable=False
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    jobs: Mapped[list["Job"]] = relationship("Job", back_populates="opportunity")

    __table_args__ = (
        UniqueConstraint("adapter_id", "external_id", name="uq_adapter_external"),
        Index("ix_opportunities_status", "status"),
        Index("ix_opportunities_category", "category"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    opportunity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    adapter_id: Mapped[str] = mapped_column(String(100), nullable=False)
    workload_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.CREATED, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    requested_resources: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    expected_revenue_usd: Mapped[Optional[float]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    expected_cost_usd: Mapped[Optional[float]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    expected_profit_usd: Mapped[Optional[float]] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    max_runtime_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    opportunity: Mapped[Optional["Opportunity"]] = relationship(
        "Opportunity", back_populates="jobs"
    )
    customer: Mapped[Optional["User"]] = relationship("User")
    assignment: Mapped[Optional["JobAssignment"]] = relationship(
        "JobAssignment", back_populates="job", uselist=False
    )
    result: Mapped[Optional["JobResult"]] = relationship(
        "JobResult", back_populates="job", uselist=False
    )
    verification: Mapped[Optional["JobVerification"]] = relationship(
        "JobVerification", back_populates="job", uselist=False
    )
    events: Mapped[list["JobEvent"]] = relationship(
        "JobEvent", back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_jobs_status_priority", "status", "priority", "created_at"),
        Index("ix_jobs_opportunity_id", "opportunity_id"),
        Index("ix_jobs_customer_id", "customer_id"),
        Index("ix_jobs_adapter_id", "adapter_id"),
    )


class JobAssignment(Base):
    __tablename__ = "job_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    reserved_cpu_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    reserved_ram_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reserved_storage_mb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reserved_bandwidth_mbps: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    job: Mapped["Job"] = relationship("Job", back_populates="assignment")
    device: Mapped["Device"] = relationship("Device", back_populates="job_assignments")


class JobResult(Base):
    __tablename__ = "job_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    result_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    result_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_metrics: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["Job"] = relationship("Job", back_populates="result")
    device: Mapped["Device"] = relationship("Device")


class JobVerification(Base):
    __tablename__ = "job_verifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    method: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["Job"] = relationship("Job", back_populates="verification")


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    to_status: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_type: Mapped[JobEventActorType] = mapped_column(
        Enum(JobEventActorType, native_enum=False), nullable=False
    )
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["Job"] = relationship("Job", back_populates="events")

    __table_args__ = (
        Index("ix_job_events_job_id", "job_id"),
        Index("ix_job_events_created_at", "created_at"),
    )


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_type: Mapped[str] = mapped_column(String(50), nullable=False)
    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    account_type: Mapped[LedgerAccountType] = mapped_column(
        Enum(LedgerAccountType, native_enum=False), nullable=False
    )
    asset: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    entries: Mapped[list["LedgerEntry"]] = relationship(
        "LedgerEntry", back_populates="account"
    )

    __table_args__ = (
        Index("ix_ledger_accounts_owner", "owner_type", "owner_id"),
        Index("ix_ledger_accounts_type_asset", "account_type", "asset"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[LedgerDirection] = mapped_column(
        Enum(LedgerDirection, native_enum=False), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    asset: Mapped[str] = mapped_column(String(20), nullable=False)
    usd_value: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    reference_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_metadata: Mapped[Any] = mapped_column("metadata", JSON, default=dict, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped["LedgerAccount"] = relationship("LedgerAccount", back_populates="entries")

    __table_args__ = (
        Index("ix_ledger_entries_transaction", "transaction_id"),
        Index("ix_ledger_entries_reference", "reference_type", "reference_id"),
        Index("ix_ledger_entries_posted_at", "posted_at"),
    )


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    asset: Mapped[str] = mapped_column(String(20), nullable=False)
    network: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    status: Mapped[WithdrawalStatus] = mapped_column(
        Enum(WithdrawalStatus, native_enum=False), default=WithdrawalStatus.REQUESTED, nullable=False
    )
    risk_score: Mapped[float] = mapped_column(Numeric(6, 5), default=0.0, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    eligible_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    broadcast_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tx_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_withdrawals_provider_id", "provider_id"),
        Index("ix_withdrawals_status", "status"),
        Index("ix_withdrawals_requested_at", "requested_at"),
    )


class ProviderBalance(Base):
    __tablename__ = "provider_balances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    pending_balance: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    available_balance: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    held_balance: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    lifetime_earned: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    lifetime_paid: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_provider_balances_provider_id", "provider_id"),
    )
