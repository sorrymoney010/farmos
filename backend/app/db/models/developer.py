"""Developer / customer platform (Sprint 16; spec §40, §101-§104, §134-§136, §170-§173).

FARMOS's own demand side: developer accounts, scoped API keys, price quotes,
deployments (one-off or recurring workloads), per-run prepaid reservations and
charges, Stripe top-ups + webhook event log, generic idempotency records and
customer result-webhook deliveries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                                               nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")  # ACTIVE | ON_HOLD | CLOSED
    hold_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = _created()


class CustomerApiKey(Base):
    """Spec §134: only an HMAC of the key is stored; the secret is shown once."""

    __tablename__ = "customer_api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
                                                   nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PriceQuote(Base):
    __tablename__ = "price_quotes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
                                                   nullable=False, index=True)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    workload_type: Mapped[str] = mapped_column(String(50), nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    maximum_cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    estimated_runtime_seconds: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    fcu: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    breakdown: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
                                                   nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    workload_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    resources: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    schedule: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    destination: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ACTIVE")
    status_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    book: Mapped[str] = mapped_column(String(10), nullable=False)
    quote_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    price_per_run_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    max_cost_per_run_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    runs_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_deployments_customer", "customer_id", "created_at"),
        Index("ix_deployments_due", "status", "next_run_at"),
    )


class CustomerJobCharge(Base):
    """One per deployment run (job): prepaid reservation -> charge (on verification) or release."""

    __tablename__ = "customer_job_charges"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
                                              nullable=False, unique=True)
    deployment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("deployments.id", ondelete="CASCADE"),
                                                     nullable=False, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
                                                   nullable=False)
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    book: Mapped[str] = mapped_column(String(10), nullable=False)
    reserved_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    price_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    charged_usd: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RESERVED")  # RESERVED|CHARGED|RELEASED
    status_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ledger_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_customer_job_charges_customer_status", "customer_id", "status"),)


class CustomerTopup(Base):
    __tablename__ = "customer_topups"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
                                                   nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd")
    method: Mapped[str] = mapped_column(String(20), nullable=False)  # checkout | payment_intent
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CREATED")
    livemode: Mapped[bool] = mapped_column(nullable=False, default=False)
    book: Mapped[str] = mapped_column(String(10), nullable=False)
    stripe_checkout_session_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    funded_usd: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    refunded_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    ledger_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    funded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class StripeWebhookEvent(Base):
    """Spec §104 webhook event log; ``stripe_event_id`` uniqueness is the idempotency guard."""

    __tablename__ = "stripe_webhook_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    stripe_event_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    livemode: Mapped[bool] = mapped_column(nullable=False, default=False)
    received_at: Mapped[datetime] = _created()
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVED")
    related_customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    related_topup_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class IdempotencyRecord(Base):
    """Spec §136: duplicate POST with the same Idempotency-Key returns the prior result."""

    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope: Mapped[str] = mapped_column(String(100), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),)


class CustomerWebhookDelivery(Base):
    """Spec §135 result-destination deliveries: signed, retried with backoff, logged."""

    __tablename__ = "customer_webhook_deliveries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    deployment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("deployments.id", ondelete="CASCADE"),
                                                     nullable=False, index=True)
    job_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")  # PENDING|DELIVERED|FAILED
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint("deployment_id", "job_id", "event_type", name="uq_customer_webhook_event"),
        Index("ix_customer_webhook_due", "status", "next_attempt_at"),
    )


__all__ = ["Customer", "CustomerApiKey", "PriceQuote", "Deployment", "CustomerJobCharge", "CustomerTopup",
           "StripeWebhookEvent", "IdempotencyRecord", "CustomerWebhookDelivery"]
