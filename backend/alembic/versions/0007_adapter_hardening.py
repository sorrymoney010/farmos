"""Sprint 10: external adapter hardening columns (spec §126, §127, §139).

Adds call accounting, circuit breaker state, granular per-adapter switches,
certification record and last simulated claim to ``revenue_adapters``.
Idempotent (``ADD COLUMN IF NOT EXISTS``) so it is safe on databases created by
``create_all``.

Revision ID: 0007_adapter_hardening
Revises: 0006_ledger_withdrawals
"""

from alembic import op


revision = "0007_adapter_hardening"
down_revision = "0006_ledger_withdrawals"
branch_labels = None
depends_on = None

COLUMNS = [
    ("call_count", "INTEGER NOT NULL DEFAULT 0"),
    ("error_count", "INTEGER NOT NULL DEFAULT 0"),
    ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("last_error", "TEXT"),
    ("last_error_at", "TIMESTAMP WITH TIME ZONE"),
    ("circuit_open_until", "TIMESTAMP WITH TIME ZONE"),
    ("switches", "JSON NOT NULL DEFAULT '{}'"),
    ("certification", "JSON NOT NULL DEFAULT '{}'"),
    ("last_claim", "JSON NOT NULL DEFAULT '{}'"),
]


def upgrade() -> None:
    for name, ddl in COLUMNS:
        op.execute(f"ALTER TABLE revenue_adapters ADD COLUMN IF NOT EXISTS {name} {ddl}")


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.execute(f"ALTER TABLE revenue_adapters DROP COLUMN IF EXISTS {name}")
