"""Phase 4: double-entry ledger (chart of accounts, typed transactions, books),
revenue confirmation, SIWE wallet verification, controlled withdrawals,
simulated payouts, reconciliation, daily close, risk events.

Idempotent (safe on databases created by ``create_all``). Legacy single-sided
device TEST credits are backfilled into balanced TEST_CREDIT transactions
before the append-only triggers are installed.

Revision ID: 0006_ledger_withdrawals
Revises: 0005_opps_economics
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0006_ledger_withdrawals"
down_revision = "0005_opps_economics"
branch_labels = None
depends_on = None

NEW_TABLES_SQL = [
    """CREATE TABLE IF NOT EXISTS ledger_transactions (
        id UUID NOT NULL PRIMARY KEY,
        tx_type VARCHAR(40) NOT NULL,
        book VARCHAR(10) NOT NULL,
        idempotency_key VARCHAR(200) NOT NULL UNIQUE,
        reference_type VARCHAR(50),
        reference_id UUID,
        description TEXT,
        actor_type VARCHAR(20) NOT NULL,
        actor_id UUID,
        reverses_transaction_id UUID,
        metadata JSON NOT NULL,
        posted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_ledger_tx_posted_at ON ledger_transactions (posted_at)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_tx_type ON ledger_transactions (tx_type)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_tx_reversal ON ledger_transactions (reverses_transaction_id) "
    "WHERE reverses_transaction_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_ledger_tx_reference ON ledger_transactions (reference_type, reference_id)",
    """CREATE TABLE IF NOT EXISTS payouts (
        id UUID NOT NULL PRIMARY KEY,
        withdrawal_id UUID NOT NULL UNIQUE,
        provider_id UUID NOT NULL,
        batch_id UUID,
        book VARCHAR(10) NOT NULL,
        asset VARCHAR(20) NOT NULL,
        network VARCHAR(50) NOT NULL,
        chain_id INTEGER NOT NULL,
        token_contract VARCHAR(100),
        recipient VARCHAR(100) NOT NULL,
        amount_atomic BIGINT NOT NULL,
        approval_hash VARCHAR(80) NOT NULL,
        signer VARCHAR(40) NOT NULL,
        signature VARCHAR(200),
        provider_name VARCHAR(40) NOT NULL,
        simulated BOOLEAN NOT NULL,
        status VARCHAR(20) NOT NULL,
        tx_hash VARCHAR(100) UNIQUE,
        block_number BIGINT,
        confirmations INTEGER NOT NULL,
        failure_reason TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        broadcast_at TIMESTAMP WITH TIME ZONE,
        confirmed_at TIMESTAMP WITH TIME ZONE)""",
    "CREATE INDEX IF NOT EXISTS ix_payouts_status ON payouts (status)",
    "CREATE INDEX IF NOT EXISTS ix_payouts_created_at ON payouts (created_at)",
    """CREATE TABLE IF NOT EXISTS sim_chain_balances (
        id SERIAL NOT NULL PRIMARY KEY,
        network VARCHAR(50) NOT NULL,
        address VARCHAR(100) NOT NULL,
        asset VARCHAR(20) NOT NULL,
        balance_atomic BIGINT NOT NULL,
        CONSTRAINT uq_sim_chain_balance UNIQUE (network, address, asset))""",
    """CREATE TABLE IF NOT EXISTS sim_chain_txs (
        tx_hash VARCHAR(100) NOT NULL PRIMARY KEY,
        network VARCHAR(50) NOT NULL,
        kind VARCHAR(20) NOT NULL,
        from_address VARCHAR(100),
        to_address VARCHAR(100) NOT NULL,
        asset VARCHAR(20) NOT NULL,
        amount_atomic BIGINT NOT NULL,
        status VARCHAR(20) NOT NULL,
        polls INTEGER NOT NULL,
        block_number BIGINT,
        reference VARCHAR(100),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS reconciliation_runs (
        id UUID NOT NULL PRIMARY KEY,
        book VARCHAR(10) NOT NULL,
        status VARCHAR(20) NOT NULL,
        checks JSON NOT NULL,
        actions JSON NOT NULL,
        triggered_by UUID,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_recon_runs_created_at ON reconciliation_runs (created_at)",
    """CREATE TABLE IF NOT EXISTS daily_closes (
        id UUID NOT NULL PRIMARY KEY,
        close_date DATE NOT NULL,
        book VARCHAR(10) NOT NULL,
        report JSON NOT NULL,
        reconciliation_status VARCHAR(20) NOT NULL,
        reconciliation_run_id UUID,
        closed_by UUID,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT uq_daily_close UNIQUE (close_date, book))""",
    """CREATE TABLE IF NOT EXISTS risk_events (
        id UUID NOT NULL PRIMARY KEY,
        category VARCHAR(30) NOT NULL,
        severity VARCHAR(10) NOT NULL,
        subject_type VARCHAR(30),
        subject_id UUID,
        message TEXT NOT NULL,
        details JSON NOT NULL,
        status VARCHAR(10) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        resolved_at TIMESTAMP WITH TIME ZONE,
        resolved_by UUID,
        resolution TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_risk_events_status ON risk_events (status)",
    "CREATE INDEX IF NOT EXISTS ix_risk_events_subject ON risk_events (subject_type, subject_id)",
]
NEW_TABLES = ["risk_events", "daily_closes", "reconciliation_runs", "sim_chain_txs", "sim_chain_balances", "payouts",
              "ledger_transactions"]

COLUMNS = {
    "job_economics": [
        sa.Column("ledger_book", sa.String(10), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_source", sa.String(50), nullable=True),
        sa.Column("confirmation_ref", sa.String(100), nullable=True),
        sa.Column("confirmed_gross_usd", sa.Numeric(30, 12), nullable=True),
        sa.Column("provider_id", UUID(as_uuid=True), nullable=True),
        sa.Column("provider_share_usd", sa.Numeric(30, 12), nullable=True),
        sa.Column("revenue_transaction_id", UUID(as_uuid=True), nullable=True),
        sa.Column("earnings_available_at", sa.DateTime(timezone=True), nullable=True),
    ],
    "ledger_accounts": [
        sa.Column("code", sa.String(10), nullable=True),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column("category", sa.String(20), nullable=True),
        sa.Column("normal_side", sa.String(6), nullable=True),
        sa.Column("book", sa.String(10), nullable=False, server_default="TEST"),
    ],
    "ledger_entries": [sa.Column("book", sa.String(10), nullable=False, server_default="TEST")],
    "provider_balances": [
        sa.Column("book", sa.String(10), nullable=False, server_default="TEST"),
        sa.Column("asset", sa.String(20), nullable=False, server_default="TEST_USD"),
    ],
    "wallet_verification_challenges": [
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("chain_id", sa.Integer(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
    ],
    "wallets": [
        sa.Column("chain_id", sa.Integer(), nullable=True),
        sa.Column("verification_method", sa.String(40), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    ],
    "withdrawals": [
        sa.Column("book", sa.String(10), nullable=False, server_default="TEST"),
        sa.Column("requested_by", UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("recipient_address", sa.String(100), nullable=True),
        sa.Column("chain_id", sa.Integer(), nullable=True),
        sa.Column("amount_atomic", sa.BigInteger(), nullable=True),
        sa.Column("risk_decision", sa.String(20), nullable=True),
        sa.Column("risk_reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("approvals", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("required_approvals", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("approval_hash", sa.String(80), nullable=True),
        sa.Column("hold_transaction_id", UUID(as_uuid=True), nullable=True),
        sa.Column("settle_transaction_id", UUID(as_uuid=True), nullable=True),
        sa.Column("release_transaction_id", UUID(as_uuid=True), nullable=True),
        sa.Column("payout_id", UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by", UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    ],
}

IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION farmos_ledger_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ledger rows are append-only (%): post a REVERSAL instead', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""
IMMUTABLE_TABLES = ("ledger_transactions", "ledger_entries")

# Legacy (pre-Phase-4) device test credits were single-sided CREDITs on DEVICE_EARNINGS_TEST
# accounts (asset USD). Give them chart code 9010, a TEST_CREDIT header and a balancing
# Dr 9000 (Test Credit Issuance, platform contra) line so the trial balance is exact.
BACKFILL_SQL = [
    """UPDATE ledger_accounts SET code = '9010', name = 'Device Test Earnings (legacy)', category = 'LIABILITY',
           normal_side = 'CREDIT', book = 'TEST'
       WHERE account_type = 'DEVICE_EARNINGS_TEST' AND code IS NULL""",
    "UPDATE ledger_accounts SET book = 'REAL' WHERE account_type = 'DEVICE_EARNINGS_REAL' AND code IS NULL",
    """INSERT INTO ledger_accounts (id, owner_type, owner_id, account_type, asset, code, name, category, normal_side,
                                   book, created_at)
       SELECT gen_random_uuid(), 'PLATFORM', '00000000-0000-0000-0000-000000000000', 'CHART_ACCOUNT', 'USD',
              '9000', 'Test Credit Issuance (contra)', 'CONTRA', 'DEBIT', 'TEST', now()
       WHERE EXISTS (SELECT 1 FROM ledger_accounts WHERE code = '9010')
         AND NOT EXISTS (SELECT 1 FROM ledger_accounts WHERE code = '9000' AND book = 'TEST' AND asset = 'USD'
                         AND owner_type = 'PLATFORM')""",
    """INSERT INTO ledger_transactions (id, tx_type, book, idempotency_key, reference_type, reference_id,
                                       description, actor_type, metadata, posted_at)
       SELECT e.transaction_id, 'TEST_CREDIT', 'TEST', 'legacy:' || e.transaction_id::text,
              min(e.reference_type), (array_agg(e.reference_id))[1], 'legacy device test credit (backfilled)',
              'SYSTEM', '{"backfilled": true}', min(e.posted_at)
       FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
       WHERE a.code = '9010'
         AND NOT EXISTS (SELECT 1 FROM ledger_transactions t WHERE t.id = e.transaction_id)
       GROUP BY e.transaction_id""",
    """INSERT INTO ledger_entries (id, transaction_id, account_id, direction, amount, asset, usd_value,
                                  reference_type, reference_id, metadata, book, posted_at)
       SELECT gen_random_uuid(), x.transaction_id,
              (SELECT id FROM ledger_accounts WHERE code = '9000' AND book = 'TEST' AND asset = 'USD'
                 AND owner_type = 'PLATFORM' LIMIT 1),
              'DEBIT', x.net, 'USD', x.net, x.reference_type, x.reference_id,
              '{"tx_type": "TEST_CREDIT", "code": "9000", "backfilled": true}', 'TEST', x.posted_at
       FROM (SELECT e.transaction_id, min(e.reference_type) AS reference_type,
                    (array_agg(e.reference_id))[1] AS reference_id, min(e.posted_at) AS posted_at,
                    sum(CASE WHEN e.direction = 'CREDIT' THEN e.amount ELSE -e.amount END) AS net
             FROM ledger_entries e
             WHERE e.transaction_id IN (SELECT e2.transaction_id FROM ledger_entries e2
                                        JOIN ledger_accounts a2 ON a2.id = e2.account_id WHERE a2.code = '9010')
             GROUP BY e.transaction_id) x
       WHERE x.net > 0""",
]


def _cols(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in NEW_TABLES_SQL:
        op.execute(stmt)
    insp = sa.inspect(bind)
    for table, cols in COLUMNS.items():
        existing = _cols(insp, table)
        for col in cols:
            if col.name not in existing:
                op.add_column(table, col.copy())

    op.alter_column("ledger_accounts", "account_type", type_=sa.String(40), existing_nullable=False)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_accounts_chart ON ledger_accounts "
               "(book, code, owner_type, owner_id, asset) WHERE code IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ledger_entries_account ON ledger_entries (account_id)")

    # provider balances: one row per (provider, book) instead of one per provider
    insp = sa.inspect(bind)
    uniques = {u["name"] for u in insp.get_unique_constraints("provider_balances")}
    if "provider_balances_provider_id_key" in uniques:
        op.drop_constraint("provider_balances_provider_id_key", "provider_balances", type_="unique")
    if "uq_provider_balance_book" not in uniques:
        op.create_unique_constraint("uq_provider_balance_book", "provider_balances", ["provider_id", "book"])
    if "uq_withdrawal_idempotency" not in {u["name"] for u in insp.get_unique_constraints("withdrawals")}:
        op.create_unique_constraint("uq_withdrawal_idempotency", "withdrawals", ["provider_id", "idempotency_key"])

    # backfill legacy single-sided postings BEFORE the append-only triggers exist
    for t in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_immutable ON {t}")
    for stmt in BACKFILL_SQL:
        op.execute(stmt)
    op.execute(IMMUTABLE_FN)
    for t in IMMUTABLE_TABLES:
        op.execute(f"CREATE TRIGGER trg_{t}_immutable BEFORE UPDATE OR DELETE ON {t} "
                   "FOR EACH ROW EXECUTE FUNCTION farmos_ledger_immutable()")


def downgrade() -> None:
    bind = op.get_bind()
    for t in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_immutable ON {t}")
    op.execute("DROP FUNCTION IF EXISTS farmos_ledger_immutable()")
    # remove backfilled contra lines (legacy single-sided shape is restored)
    op.execute("""DELETE FROM ledger_entries WHERE account_id IN
                  (SELECT id FROM ledger_accounts WHERE code IS NOT NULL AND code <> '9010')""")
    op.execute("DELETE FROM ledger_accounts WHERE code IS NOT NULL AND code <> '9010'")
    for t in NEW_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t}")
    insp = sa.inspect(bind)
    if "uq_withdrawal_idempotency" in {u["name"] for u in insp.get_unique_constraints("withdrawals")}:
        op.drop_constraint("uq_withdrawal_idempotency", "withdrawals", type_="unique")
    uniques = {u["name"] for u in insp.get_unique_constraints("provider_balances")}
    if "uq_provider_balance_book" in uniques:
        op.drop_constraint("uq_provider_balance_book", "provider_balances", type_="unique")
    op.execute("DELETE FROM provider_balances WHERE book <> 'TEST'")
    if "provider_balances_provider_id_key" not in uniques:
        op.create_unique_constraint("provider_balances_provider_id_key", "provider_balances", ["provider_id"])
    op.execute("DROP INDEX IF EXISTS ix_ledger_entries_account")
    op.execute("DROP INDEX IF EXISTS uq_ledger_accounts_chart")
    for table, cols in COLUMNS.items():
        existing = _cols(insp, table)
        for col in reversed(cols):
            if col.name in existing:
                op.drop_column(table, col.name)
    op.alter_column("ledger_accounts", "account_type", type_=sa.String(23), existing_nullable=False)
