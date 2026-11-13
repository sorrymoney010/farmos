"""Sprint 16: developer platform - customers, scoped API keys, quotes,
deployments, prepaid charges, Stripe top-ups + webhook events, idempotency
records, customer webhook deliveries.

Idempotent (IF NOT EXISTS) so it is safe on databases created by ``create_all``.

Revision ID: 0008_developer_platform
Revises: 0007_adapter_hardening
"""

from alembic import op


revision = "0008_developer_platform"
down_revision = "0007_adapter_hardening"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS customers (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	user_id UUID NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	hold_reason TEXT, 
	stripe_customer_id VARCHAR(100), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
    """,
    """
CREATE TABLE IF NOT EXISTS customer_api_keys (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	customer_id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	prefix VARCHAR(32) NOT NULL, 
	secret_hash VARCHAR(128) NOT NULL, 
	scopes JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE CASCADE, 
	UNIQUE (prefix)
)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_customer_api_keys_customer_id ON customer_api_keys (customer_id)
    """,
    """
CREATE TABLE IF NOT EXISTS price_quotes (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	customer_id UUID NOT NULL, 
	spec_hash VARCHAR(64) NOT NULL, 
	workload_type VARCHAR(50) NOT NULL, 
	estimated_cost_usd NUMERIC(18, 8) NOT NULL, 
	maximum_cost_usd NUMERIC(18, 8) NOT NULL, 
	estimated_runtime_seconds NUMERIC(12, 3) NOT NULL, 
	fcu NUMERIC(14, 6) NOT NULL, 
	breakdown JSON NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE CASCADE
)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_price_quotes_customer_id ON price_quotes (customer_id)
    """,
    """
CREATE TABLE IF NOT EXISTS deployments (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	customer_id UUID NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	workload_type VARCHAR(50) NOT NULL, 
	payload JSON NOT NULL, 
	resources JSON NOT NULL, 
	schedule JSON NOT NULL, 
	destination JSON NOT NULL, 
	webhook_secret VARCHAR(100), 
	status VARCHAR(30) NOT NULL, 
	status_reason TEXT, 
	book VARCHAR(10) NOT NULL, 
	quote_id UUID, 
	price_per_run_usd NUMERIC(18, 8) NOT NULL, 
	max_cost_per_run_usd NUMERIC(18, 8) NOT NULL, 
	priority INTEGER NOT NULL, 
	runs_created INTEGER NOT NULL, 
	next_run_at TIMESTAMP WITH TIME ZONE, 
	last_run_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	cancelled_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE CASCADE
)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_deployments_customer ON deployments (customer_id, created_at)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_deployments_due ON deployments (status, next_run_at)
    """,
    """
CREATE TABLE IF NOT EXISTS customer_job_charges (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	job_id UUID NOT NULL, 
	deployment_id UUID NOT NULL, 
	customer_id UUID NOT NULL, 
	run_index INTEGER NOT NULL, 
	book VARCHAR(10) NOT NULL, 
	reserved_usd NUMERIC(18, 8) NOT NULL, 
	price_usd NUMERIC(18, 8) NOT NULL, 
	charged_usd NUMERIC(18, 8), 
	status VARCHAR(20) NOT NULL, 
	status_reason TEXT, 
	ledger_transaction_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	settled_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (job_id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(deployment_id) REFERENCES deployments (id) ON DELETE CASCADE, 
	FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE CASCADE
)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_customer_job_charges_deployment_id ON customer_job_charges (deployment_id)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_customer_job_charges_customer_status ON customer_job_charges (customer_id, status)
    """,
    """
CREATE TABLE IF NOT EXISTS customer_topups (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	customer_id UUID NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	method VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	livemode BOOLEAN NOT NULL, 
	book VARCHAR(10) NOT NULL, 
	stripe_checkout_session_id VARCHAR(255), 
	stripe_payment_intent_id VARCHAR(255), 
	checkout_url TEXT, 
	funded_usd NUMERIC(18, 8), 
	refunded_usd NUMERIC(18, 8) NOT NULL, 
	ledger_transaction_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	funded_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE CASCADE, 
	UNIQUE (stripe_checkout_session_id), 
	UNIQUE (stripe_payment_intent_id)
)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_customer_topups_customer_id ON customer_topups (customer_id)
    """,
    """
CREATE TABLE IF NOT EXISTS stripe_webhook_events (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	stripe_event_id VARCHAR(255) NOT NULL, 
	event_type VARCHAR(100) NOT NULL, 
	livemode BOOLEAN NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE, 
	processing_status VARCHAR(20) NOT NULL, 
	related_customer_id UUID, 
	related_topup_id UUID, 
	payload_hash VARCHAR(64) NOT NULL, 
	detail TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (stripe_event_id)
)
    """,
    """
CREATE TABLE IF NOT EXISTS idempotency_records (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	scope VARCHAR(100) NOT NULL, 
	key VARCHAR(200) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	status_code INTEGER NOT NULL, 
	response JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_idempotency_scope_key UNIQUE (scope, key)
)
    """,
    """
CREATE TABLE IF NOT EXISTS customer_webhook_deliveries (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	deployment_id UUID NOT NULL, 
	job_id UUID, 
	event_type VARCHAR(50) NOT NULL, 
	url TEXT NOT NULL, 
	payload JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	attempts INTEGER NOT NULL, 
	next_attempt_at TIMESTAMP WITH TIME ZONE, 
	last_status_code INTEGER, 
	last_error TEXT, 
	delivered_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_customer_webhook_event UNIQUE (deployment_id, job_id, event_type), 
	FOREIGN KEY(deployment_id) REFERENCES deployments (id) ON DELETE CASCADE
)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_customer_webhook_due ON customer_webhook_deliveries (status, next_attempt_at)
    """,
    """
CREATE INDEX IF NOT EXISTS ix_customer_webhook_deliveries_deployment_id ON customer_webhook_deliveries (deployment_id)
    """,
]

TABLES = ["customer_webhook_deliveries", "idempotency_records", "stripe_webhook_events", "customer_topups",
          "customer_job_charges", "deployments", "price_quotes", "customer_api_keys", "customers"]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
