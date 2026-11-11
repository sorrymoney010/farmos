"""Phase 3: adapter registry/kill switch, opportunity profitability inputs,
job scheduling constraints, per-job and per-device economics.

Revision ID: 0005_opps_economics
Revises: 0004_job_leases
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0005_opps_economics"
down_revision = "0004_job_leases"
branch_labels = None
depends_on = None

OPP_COLUMNS = [
    sa.Column("workload_type", sa.String(100), nullable=True),
    sa.Column("revenue_usd_per_job", sa.Numeric(18, 8), nullable=True),
    sa.Column("expected_runtime_seconds", sa.Integer(), nullable=True),
    sa.Column("thermal_intensity", sa.Numeric(6, 4), nullable=False, server_default="0.3"),
    sa.Column("repeatability_score", sa.Numeric(6, 5), nullable=False, server_default="0.8"),
    sa.Column("expected_payment_days", sa.Numeric(8, 2), nullable=False, server_default="0"),
    sa.Column("normalized_resource_consumption", sa.Numeric(6, 4), nullable=False, server_default="0.5"),
    sa.Column("estimated_api_cost_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
    sa.Column("expected_network_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    sa.Column("payload_template", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("disabled_reason", sa.Text(), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
]


def _add_missing(table, columns, insp):
    existing = {c["name"] for c in insp.get_columns(table)}
    for col in columns:
        if col.name not in existing:
            op.add_column(table, col.copy())


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    _add_missing("opportunities", OPP_COLUMNS, insp)
    _add_missing("jobs", [sa.Column("constraints", sa.JSON(), nullable=False, server_default="{}")], insp)

    if not insp.has_table("revenue_adapters"):
        op.create_table(
            "revenue_adapters",
            sa.Column("id", sa.String(100), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("category", sa.String(100), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("disabled_reason", sa.Text(), nullable=True),
            sa.Column("disabled_by", UUID(as_uuid=True), nullable=True),
            sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("certification_stage", sa.String(40), nullable=False, server_default="SANDBOX"),
            sa.Column("health_status", sa.String(20), nullable=False, server_default="UNKNOWN"),
            sa.Column("health_detail", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_reconcile", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if not insp.has_table("job_economics"):
        n = lambda name: sa.Column(name, sa.Numeric(18, 8), nullable=False, server_default="0")  # noqa: E731
        op.create_table(
            "job_economics",
            sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("opportunity_id", UUID(as_uuid=True), nullable=True),
            sa.Column("adapter_id", sa.String(100), nullable=True),
            sa.Column("device_id", UUID(as_uuid=True), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="ESTIMATED"),
            sa.Column("asset_symbol", sa.String(20), nullable=True),
            n("expected_gross_usd"), n("expected_adjusted_gross_usd"), n("expected_cost_usd"), n("expected_net_usd"),
            sa.Column("expected_costs", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("expected_runtime_seconds", sa.Numeric(12, 3), nullable=False, server_default="0"),
            n("opportunity_score"),
            sa.Column("device_fit", sa.Numeric(8, 5), nullable=True),
            sa.Column("final_score", sa.Numeric(18, 8), nullable=True),
            sa.Column("score_inputs", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("actual_runtime_seconds", sa.Numeric(12, 3), nullable=True),
            sa.Column("actual_gross_usd", sa.Numeric(18, 8), nullable=True),
            sa.Column("actual_cost_usd", sa.Numeric(18, 8), nullable=True),
            sa.Column("actual_net_usd", sa.Numeric(18, 8), nullable=True),
            sa.Column("actual_costs", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("revenue_state", sa.String(30), nullable=False, server_default="ESTIMATED"),
            sa.Column("estimated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_job_economics_device_id", "job_economics", ["device_id"])
        op.create_index("ix_job_economics_status", "job_economics", ["status"])

    if not insp.has_table("device_economics"):
        z = lambda name, p=(18, 8): sa.Column(name, sa.Numeric(*p), nullable=False, server_default="0")  # noqa: E731
        op.create_table(
            "device_economics",
            sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("purchase_price_usd", sa.Numeric(12, 2), nullable=True),
            sa.Column("resale_value_usd", sa.Numeric(12, 2), nullable=True),
            z("maintenance_usd", (12, 4)),
            sa.Column("active_watts", sa.Numeric(8, 3), nullable=True),
            z("lifetime_gross_usd"), z("electricity_usd"), z("network_usd"), z("infrastructure_usd"),
            z("depreciation_usd"), z("other_costs_usd"), z("net_usd"),
            sa.Column("jobs_completed", sa.Integer(), nullable=False, server_default="0"),
            z("runtime_seconds", (14, 3)),
            sa.Column("first_job_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_job_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # Legacy adapter id for internally created jobs.
    op.execute("UPDATE jobs SET adapter_id = CASE WHEN workload_type = 'benchmark' THEN 'system' "
               "ELSE 'internal_compute' END WHERE adapter_id = 'internal'")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for t in ("device_economics", "job_economics", "revenue_adapters"):
        if insp.has_table(t):
            op.drop_table(t)
    if "constraints" in {c["name"] for c in insp.get_columns("jobs")}:
        op.drop_column("jobs", "constraints")
    existing = {c["name"] for c in insp.get_columns("opportunities")}
    for col in reversed(OPP_COLUMNS):
        if col.name in existing:
            op.drop_column("opportunities", col.name)
