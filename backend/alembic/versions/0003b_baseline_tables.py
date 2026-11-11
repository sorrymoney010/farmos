"""Baseline: create the core tables that existed before Phase 2 but were never
in a migration (they were only ever created by ``create_all``), so a fresh
``alembic upgrade head`` on an empty database builds the full schema.

Shapes are frozen as of revision 0003 (phase 1). Later revisions add their
columns idempotently. Everything here is ``IF NOT EXISTS``, so databases that
were created with ``create_all`` (all existing environments) are unaffected.

Revision ID: 0003b_baseline_tables
Revises: 0003_device_safety
"""

from alembic import op
import sqlalchemy as sa


revision = "0003b_baseline_tables"
down_revision = "0003_device_safety"
branch_labels = None
depends_on = None

BASELINE_TABLES = ['ledger_accounts', 'opportunities', 'jobs', 'ledger_entries', 'job_events', 'job_verifications', 'provider_balances', 'wallets', 'job_assignments', 'job_results', 'wallet_verification_challenges', 'withdrawals']

DDL = [
    """CREATE TABLE IF NOT EXISTS ledger_accounts (
	id UUID NOT NULL,
	owner_type VARCHAR(50) NOT NULL,
	owner_id UUID,
	account_type VARCHAR(23) NOT NULL,
	asset VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
)""",
    """CREATE INDEX IF NOT EXISTS ix_ledger_accounts_owner ON ledger_accounts (owner_type, owner_id)""",
    """CREATE INDEX IF NOT EXISTS ix_ledger_accounts_type_asset ON ledger_accounts (account_type, asset)""",
    """CREATE TABLE IF NOT EXISTS opportunities (
	id UUID NOT NULL,
	adapter_id VARCHAR(100) NOT NULL,
	external_id VARCHAR(255),
	name VARCHAR(255) NOT NULL,
	category VARCHAR(100) NOT NULL,
	asset_symbol VARCHAR(20),
	estimated_revenue_usd_hour NUMERIC(18, 8),
	payment_confidence NUMERIC(6, 5) NOT NULL,
	risk_score NUMERIC(6, 5) NOT NULL,
	min_farm_score NUMERIC(8, 4),
	region_rules JSON NOT NULL,
	resource_requirements JSON NOT NULL,
	constraints JSON NOT NULL,
	available_capacity INTEGER,
	status VARCHAR(10) NOT NULL,
	discovered_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_adapter_external UNIQUE (adapter_id, external_id)
)""",
    """CREATE INDEX IF NOT EXISTS ix_opportunities_adapter_id ON opportunities (adapter_id)""",
    """CREATE INDEX IF NOT EXISTS ix_opportunities_category ON opportunities (category)""",
    """CREATE INDEX IF NOT EXISTS ix_opportunities_status ON opportunities (status)""",
    """CREATE TABLE IF NOT EXISTS jobs (
	id UUID NOT NULL,
	opportunity_id UUID,
	customer_id UUID,
	adapter_id VARCHAR(100) NOT NULL,
	workload_type VARCHAR(100) NOT NULL,
	status VARCHAR(17) NOT NULL,
	priority INTEGER NOT NULL,
	requested_resources JSON NOT NULL,
	expected_revenue_usd NUMERIC(18, 8),
	expected_cost_usd NUMERIC(18, 8),
	expected_profit_usd NUMERIC(18, 8),
	max_runtime_seconds INTEGER,
	deadline_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(opportunity_id) REFERENCES opportunities (id) ON DELETE SET NULL,
	FOREIGN KEY(customer_id) REFERENCES users (id) ON DELETE SET NULL
)""",
    """CREATE INDEX IF NOT EXISTS ix_jobs_adapter_id ON jobs (adapter_id)""",
    """CREATE INDEX IF NOT EXISTS ix_jobs_customer_id ON jobs (customer_id)""",
    """CREATE INDEX IF NOT EXISTS ix_jobs_opportunity_id ON jobs (opportunity_id)""",
    """CREATE INDEX IF NOT EXISTS ix_jobs_status_priority ON jobs (status, priority, created_at)""",
    """CREATE TABLE IF NOT EXISTS ledger_entries (
	id UUID NOT NULL,
	transaction_id UUID NOT NULL,
	account_id UUID NOT NULL,
	direction VARCHAR(6) NOT NULL,
	amount NUMERIC(30, 12) NOT NULL,
	asset VARCHAR(20) NOT NULL,
	usd_value NUMERIC(18, 8),
	reference_type VARCHAR(50) NOT NULL,
	reference_id UUID,
	metadata JSON NOT NULL,
	posted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(account_id) REFERENCES ledger_accounts (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX IF NOT EXISTS ix_ledger_entries_posted_at ON ledger_entries (posted_at)""",
    """CREATE INDEX IF NOT EXISTS ix_ledger_entries_reference ON ledger_entries (reference_type, reference_id)""",
    """CREATE INDEX IF NOT EXISTS ix_ledger_entries_transaction ON ledger_entries (transaction_id)""",
    """CREATE INDEX IF NOT EXISTS ix_ledger_entries_transaction_id ON ledger_entries (transaction_id)""",
    """CREATE TABLE IF NOT EXISTS job_events (
	id BIGSERIAL NOT NULL,
	job_id UUID NOT NULL,
	from_status VARCHAR(50),
	to_status VARCHAR(50) NOT NULL,
	reason TEXT,
	actor_type VARCHAR(8) NOT NULL,
	actor_id UUID,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
)""",
    """CREATE INDEX IF NOT EXISTS ix_job_events_created_at ON job_events (created_at)""",
    """CREATE INDEX IF NOT EXISTS ix_job_events_job_id ON job_events (job_id)""",
    """CREATE TABLE IF NOT EXISTS job_verifications (
	id UUID NOT NULL,
	job_id UUID NOT NULL,
	status VARCHAR(50) NOT NULL,
	method VARCHAR(100) NOT NULL,
	evidence JSON NOT NULL,
	verified_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (job_id),
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
)""",
    """CREATE TABLE IF NOT EXISTS provider_balances (
	id UUID NOT NULL,
	provider_id UUID NOT NULL,
	pending_balance NUMERIC(30, 12) NOT NULL,
	available_balance NUMERIC(30, 12) NOT NULL,
	held_balance NUMERIC(30, 12) NOT NULL,
	lifetime_earned NUMERIC(30, 12) NOT NULL,
	lifetime_paid NUMERIC(30, 12) NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (provider_id),
	FOREIGN KEY(provider_id) REFERENCES providers (id) ON DELETE CASCADE
)""",
    """CREATE INDEX IF NOT EXISTS ix_provider_balances_provider_id ON provider_balances (provider_id)""",
    """CREATE TABLE IF NOT EXISTS wallets (
	id UUID NOT NULL,
	user_id UUID,
	provider_id UUID,
	chain_family VARCHAR(7) NOT NULL,
	network VARCHAR(100) NOT NULL,
	address VARCHAR(255) NOT NULL,
	label VARCHAR(255),
	is_verified BOOLEAN NOT NULL,
	is_default BOOLEAN NOT NULL,
	verified_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_user_wallet UNIQUE (user_id, network, address),
	CONSTRAINT uq_provider_wallet UNIQUE (provider_id, network, address),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(provider_id) REFERENCES providers (id) ON DELETE SET NULL
)""",
    """CREATE INDEX IF NOT EXISTS ix_wallets_address ON wallets (address)""",
    """CREATE INDEX IF NOT EXISTS ix_wallets_provider_id ON wallets (provider_id)""",
    """CREATE INDEX IF NOT EXISTS ix_wallets_user_id ON wallets (user_id)""",
    """CREATE TABLE IF NOT EXISTS job_assignments (
	id UUID NOT NULL,
	job_id UUID NOT NULL,
	device_id UUID NOT NULL,
	reserved_cpu_pct NUMERIC(5, 2),
	reserved_ram_mb INTEGER,
	reserved_storage_mb BIGINT,
	reserved_bandwidth_mbps NUMERIC(10, 2),
	assigned_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	accepted_at TIMESTAMP WITH TIME ZONE,
	released_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (job_id),
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE,
	FOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE CASCADE
)""",
    """CREATE TABLE IF NOT EXISTS job_results (
	id UUID NOT NULL,
	job_id UUID NOT NULL,
	device_id UUID NOT NULL,
	result_hash VARCHAR(128) NOT NULL,
	result_uri TEXT,
	execution_metrics JSON NOT NULL,
	submitted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (job_id),
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE,
	FOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE CASCADE
)""",
    """CREATE TABLE IF NOT EXISTS wallet_verification_challenges (
	id UUID NOT NULL,
	wallet_id UUID NOT NULL,
	nonce VARCHAR(64) NOT NULL,
	message TEXT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	consumed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(wallet_id) REFERENCES wallets (id) ON DELETE CASCADE,
	UNIQUE (nonce)
)""",
    """CREATE TABLE IF NOT EXISTS withdrawals (
	id UUID NOT NULL,
	provider_id UUID NOT NULL,
	wallet_id UUID NOT NULL,
	asset VARCHAR(20) NOT NULL,
	network VARCHAR(100) NOT NULL,
	amount NUMERIC(30, 12) NOT NULL,
	status VARCHAR(13) NOT NULL,
	risk_score NUMERIC(6, 5) NOT NULL,
	requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	eligible_at TIMESTAMP WITH TIME ZONE,
	approved_at TIMESTAMP WITH TIME ZONE,
	broadcast_at TIMESTAMP WITH TIME ZONE,
	confirmed_at TIMESTAMP WITH TIME ZONE,
	tx_hash VARCHAR(255),
	failure_reason TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(provider_id) REFERENCES providers (id) ON DELETE CASCADE,
	FOREIGN KEY(wallet_id) REFERENCES wallets (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX IF NOT EXISTS ix_withdrawals_provider_id ON withdrawals (provider_id)""",
    """CREATE INDEX IF NOT EXISTS ix_withdrawals_requested_at ON withdrawals (requested_at)""",
    """CREATE INDEX IF NOT EXISTS ix_withdrawals_status ON withdrawals (status)""",
]

# indexes the models declare that 0001-0003 never created
EXTRA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_actor ON audit_logs (actor_type, actor_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_request_id ON audit_logs (request_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_target ON audit_logs (target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS ix_benchmarks_completed_at ON device_benchmarks (completed_at)",
    "CREATE INDEX IF NOT EXISTS ix_health_events_created_at ON device_health_events (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_health_events_severity ON device_health_events (severity)",
    "CREATE INDEX IF NOT EXISTS ix_heartbeats_observed_at ON device_heartbeats (observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_devices_last_seen ON devices (last_seen_at)",
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # devices.farm_id is a free-form string tag in the models (not a FK to farms.id)
    cols = {c["name"]: c for c in insp.get_columns("devices")}
    if "farm_id" in cols and isinstance(cols["farm_id"]["type"], sa.UUID):
        for fk in insp.get_foreign_keys("devices"):
            if fk.get("constrained_columns") == ["farm_id"] and fk.get("name"):
                op.drop_constraint(fk["name"], "devices", type_="foreignkey")
        op.alter_column("devices", "farm_id", type_=sa.String(100), postgresql_using="farm_id::text")
    for stmt in DDL:
        op.execute(stmt)
    for stmt in EXTRA_INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    # Intentionally a no-op: in every existing environment these tables were created
    # by create_all and hold real data (jobs, ledger, wallets). Dropping them on a
    # downgrade would be destructive, so the baseline is never undone automatically.
    pass
