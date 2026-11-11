"""Phase 4 finance models: typed double-entry transactions, payouts (simulated
chain only), reconciliation runs, daily close, platform risk events.

Ledger rows are append-only: a Postgres trigger rejects UPDATE/DELETE on
``ledger_entries`` and ``ledger_transactions`` (spec §53 rule 7: no ledger
mutation without audit trail; corrections are REVERSAL transactions).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    DDL,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class LedgerTransaction(Base):
    """Header of one balanced posting (spec §66, §100)."""

    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tx_type: Mapped[str] = mapped_column(String(40), nullable=False)
    book: Mapped[str] = mapped_column(String(10), nullable=False)  # TEST | REAL (never mixed)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    reference_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="SYSTEM")
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    reverses_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_metadata: Mapped[Any] = mapped_column("metadata", JSON, default=dict, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_ledger_tx_type", "tx_type"),
        Index("ix_ledger_tx_reference", "reference_type", "reference_id"),
        Index("ix_ledger_tx_posted_at", "posted_at"),
        Index("uq_ledger_tx_reversal", "reverses_transaction_id", unique=True,
              postgresql_where=text("reverses_transaction_id IS NOT NULL")),
    )


class Payout(Base):
    """One payout instruction (spec §108) and its chain state (spec §111)."""

    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    withdrawal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    book: Mapped[str] = mapped_column(String(10), nullable=False)
    asset: Mapped[str] = mapped_column(String(20), nullable=False)
    network: Mapped[str] = mapped_column(String(50), nullable=False)
    chain_id: Mapped[int] = mapped_column(Integer, nullable=False)
    token_contract: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    recipient: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_atomic: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approval_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    signer: Mapped[str] = mapped_column(String(40), nullable=False)
    signature: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    provider_name: Mapped[str] = mapped_column(String(40), nullable=False)
    simulated: Mapped[bool] = mapped_column(default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CREATED")
    # CREATED|SIGNED|BROADCAST|SEEN_ONCHAIN|CONFIRMING|CONFIRMED|FAILED|REJECTED
    tx_hash: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True)
    block_number: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    confirmations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    broadcast_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_payouts_status", "status"),
        Index("ix_payouts_created_at", "created_at"),
    )


class SimChainBalance(Base):
    """Simulated chain state for the simulated payout provider. Not a real chain."""

    __tablename__ = "sim_chain_balances"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    network: Mapped[str] = mapped_column(String(50), nullable=False)
    address: Mapped[str] = mapped_column(String(100), nullable=False)
    asset: Mapped[str] = mapped_column(String(20), nullable=False)
    balance_atomic: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("network", "address", "asset", name="uq_sim_chain_balance"),)


class SimChainTx(Base):
    __tablename__ = "sim_chain_txs"

    tx_hash: Mapped[str] = mapped_column(String(100), primary_key=True)
    network: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # DEPOSIT | TRANSFER
    from_address: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    to_address: Mapped[str] = mapped_column(String(100), nullable=False)
    asset: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_atomic: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    polls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    block_number: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reference: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReconciliationRun(Base):
    """Spec §137 reconciliation engine output."""

    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # OK | MISMATCH
    checks: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    actions: Mapped[Any] = mapped_column(JSON, default=list, nullable=False)
    triggered_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_recon_runs_created_at", "created_at"),)


class DailyClose(Base):
    """Spec §138 daily financial close. One per (date, book); immutable once closed."""

    __tablename__ = "daily_closes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    close_date: Mapped[date] = mapped_column(Date, nullable=False)
    book: Mapped[str] = mapped_column(String(10), nullable=False)
    report: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reconciliation_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    closed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("close_date", "book", name="uq_daily_close"),)


class RiskEvent(Base):
    """Platform/finance risk events (reconciliation deltas, withdrawal risk, wallet abuse)."""

    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")
    subject_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Any] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_risk_events_subject", "subject_type", "subject_id"),
        Index("ix_risk_events_status", "status"),
    )


# ── append-only ledger (Postgres) ────────────────────────────────────────────

IMMUTABLE_FN = DDL("""
CREATE OR REPLACE FUNCTION farmos_ledger_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ledger rows are append-only (%%): post a REVERSAL instead', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
""")


def _triggers(table: str) -> list[DDL]:
    return [DDL(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}"),
            DDL(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION farmos_ledger_immutable()")]


def _install(table_obj, name: str) -> None:
    event.listen(table_obj, "after_create", IMMUTABLE_FN.execute_if(dialect="postgresql"))
    for ddl in _triggers(name):
        event.listen(table_obj, "after_create", ddl.execute_if(dialect="postgresql"))


# ledger_entries lives in jobs_ledger; create order is not guaranteed, so each
# table creates the (idempotent) function before its own trigger.
from app.db.models.jobs_ledger import LedgerEntry  # noqa: E402

_install(LedgerTransaction.__table__, "ledger_transactions")
_install(LedgerEntry.__table__, "ledger_entries")
