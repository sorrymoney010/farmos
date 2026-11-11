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
    text,
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
    # ── Phase 3: profitability inputs (spec §13, §64) ──
    workload_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    revenue_usd_per_job: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    expected_runtime_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    thermal_intensity: Mapped[float] = mapped_column(Numeric(6, 4), default=0.3, server_default="0.3", nullable=False)
    repeatability_score: Mapped[float] = mapped_column(Numeric(6, 5), default=0.8, server_default="0.8", nullable=False)
    expected_payment_days: Mapped[float] = mapped_column(Numeric(8, 2), default=0, server_default="0", nullable=False)
    normalized_resource_consumption: Mapped[float] = mapped_column(Numeric(6, 4), default=0.5, server_default="0.5", nullable=False)
    estimated_api_cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, server_default="0", nullable=False)
    expected_network_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    payload_template: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    disabled_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
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
    # ── Phase 2: leases, retries, dead-letter, idempotency, verification ──
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, server_default="3", nullable=False)
    dead_lettered: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    parent_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    # Server-only verification data (role, known answers, excluded devices).
    # NEVER sent to devices.
    verification_spec: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    # ── Phase 3: scheduling constraints (never sent to devices) ──
    # {device_affinity: [ids], min_farm_score, cpu_pct, ram_mb, bandwidth_mbps, tier}
    constraints: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
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


class RevenueAdapterRecord(Base):
    """Registry + kill switch for revenue adapters (spec §14, §57.10, §126-127).

    Every adapter is disableable from admin without a code deployment.
    """

    __tablename__ = "revenue_adapters"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)          # INTERNAL | EXTERNAL
    simulated: Mapped[bool] = mapped_column(default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    disabled_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    disabled_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    certification_stage: Mapped[str] = mapped_column(String(40), nullable=False, default="SANDBOX")
    health_status: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    health_detail: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    last_health_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reconcile: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class JobEconomics(Base):
    """Per-job economics: estimate at match time, actuals after verification (spec §64, §65)."""

    __tablename__ = "job_economics"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    opportunity_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    adapter_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ESTIMATED")  # ESTIMATED|REJECTED|ACTUAL
    asset_symbol: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    expected_gross_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    expected_adjusted_gross_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    expected_cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    expected_net_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    expected_costs: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    expected_runtime_seconds: Mapped[float] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    opportunity_score: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    device_fit: Mapped[Optional[float]] = mapped_column(Numeric(8, 5), nullable=True)
    final_score: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    score_inputs: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    actual_runtime_seconds: Mapped[Optional[float]] = mapped_column(Numeric(12, 3), nullable=True)
    actual_gross_usd: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    actual_cost_usd: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    actual_net_usd: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    actual_costs: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    revenue_state: Mapped[str] = mapped_column(String(30), nullable=False, default="ESTIMATED")
    estimated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase 4: revenue confirmation (UNCONFIRMED/SIMULATED -> CONFIRMED|DISPUTED) and split.
    ledger_book: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    confirmation_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    confirmed_gross_usd: Mapped[Optional[float]] = mapped_column(Numeric(30, 12), nullable=True)
    provider_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_share_usd: Mapped[Optional[float]] = mapped_column(Numeric(30, 12), nullable=True)
    revenue_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    earnings_available_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_job_economics_device_id", "device_id"),
        Index("ix_job_economics_status", "status"),
    )


class DeviceEconomics(Base):
    """Per-node P&L (spec §18). Aggregates are updated once per verified job."""

    __tablename__ = "device_economics"

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    purchase_price_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    resale_value_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    maintenance_usd: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    active_watts: Mapped[Optional[float]] = mapped_column(Numeric(8, 3), nullable=True)
    lifetime_gross_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    electricity_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    network_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    infrastructure_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    depreciation_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    other_costs_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    net_usd: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    jobs_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runtime_seconds: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    first_job_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class JobLease(Base):
    """One dispatch attempt of a job to a device (spec §71).

    Holds lease_id, device_id, start, expiry and grace; also archives the
    attempt's result + verification outcome so history survives requeues
    (job_assignments / job_results / job_verifications hold the CURRENT attempt).
    """

    __tablename__ = "job_leases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    renewals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    result_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    evidence: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_job_leases_job_id", "job_id"),
        Index("ix_job_leases_device_id", "device_id"),
        Index("ix_job_leases_status_expires", "status", "expires_at"),
    )


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
        Enum(LedgerAccountType, native_enum=False, length=40), nullable=False
    )
    asset: Mapped[str] = mapped_column(String(20), nullable=False)
    # Phase 4 chart of accounts (spec §99). Legacy per-device test accounts have code 9010.
    code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)   # ASSET|LIABILITY|REVENUE|EXPENSE|RESERVE|CONTRA
    normal_side: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)  # DEBIT|CREDIT
    book: Mapped[str] = mapped_column(String(10), nullable=False, default="TEST", server_default="TEST")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    entries: Mapped[list["LedgerEntry"]] = relationship(
        "LedgerEntry", back_populates="account"
    )

    __table_args__ = (
        Index("ix_ledger_accounts_owner", "owner_type", "owner_id"),
        Index("ix_ledger_accounts_type_asset", "account_type", "asset"),
        Index("uq_ledger_accounts_chart", "book", "code", "owner_type", "owner_id", "asset", unique=True,
              postgresql_where=text("code IS NOT NULL")),
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
    book: Mapped[str] = mapped_column(String(10), nullable=False, default="TEST", server_default="TEST")
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped["LedgerAccount"] = relationship("LedgerAccount", back_populates="entries")

    __table_args__ = (
        Index("ix_ledger_entries_transaction", "transaction_id"),
        Index("ix_ledger_entries_reference", "reference_type", "reference_id"),
        Index("ix_ledger_entries_posted_at", "posted_at"),
        Index("ix_ledger_entries_account", "account_id"),
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
    # Phase 4 controls (spec §27, §74, §106-§111, §149)
    book: Mapped[str] = mapped_column(String(10), nullable=False, default="TEST", server_default="TEST")
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    recipient_address: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    chain_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    amount_atomic: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    risk_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    risk_reasons: Mapped[Any] = mapped_column(JSON, default=list, nullable=False)
    approvals: Mapped[Any] = mapped_column(JSON, default=list, nullable=False)
    required_approvals: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    approval_hash: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    hold_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    settle_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    release_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    payout_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("provider_id", "idempotency_key", name="uq_withdrawal_idempotency"),
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
        UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    book: Mapped[str] = mapped_column(String(10), nullable=False, default="TEST", server_default="TEST")
    asset: Mapped[str] = mapped_column(String(20), nullable=False, default="TEST_USD", server_default="TEST_USD")
    pending_balance: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    available_balance: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    held_balance: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    lifetime_earned: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    lifetime_paid: Mapped[float] = mapped_column(Numeric(30, 12), default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider_id", "book", name="uq_provider_balance_book"),
        Index("ix_provider_balances_provider_id", "provider_id"),
    )
