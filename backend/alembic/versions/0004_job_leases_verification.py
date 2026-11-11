"""Phase 2: job leases, retries/dead-letter, idempotency, verification spec.

Revision ID: 0004_job_leases
Revises: 0003_device_safety
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0004_job_leases"
down_revision = "0003_device_safety"
branch_labels = None
depends_on = None

JOB_COLUMNS = [
    sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
    sa.Column("dead_lettered", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("last_error", sa.Text(), nullable=True),
    sa.Column("idempotency_key", sa.String(128), nullable=True),
    sa.Column("parent_job_id", UUID(as_uuid=True), nullable=True),
    sa.Column("verification_spec", sa.JSON(), nullable=False, server_default="{}"),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("jobs")}
    for col in JOB_COLUMNS:
        if col.name not in existing:
            op.add_column("jobs", col.copy())
    if "idempotency_key" not in existing:
        op.create_unique_constraint("uq_jobs_idempotency_key", "jobs", ["idempotency_key"])
    if "parent_job_id" not in existing:
        op.create_foreign_key("fk_jobs_parent_job_id", "jobs", "jobs", ["parent_job_id"], ["id"],
                              ondelete="SET NULL")

    if not insp.has_table("job_leases"):
        op.create_table(
            "job_leases",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("grace_seconds", sa.Integer(), nullable=False),
            sa.Column("renewals", sa.Integer(), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("outcome", sa.String(40), nullable=True),
            sa.Column("result_hash", sa.String(128), nullable=True),
            sa.Column("evidence", sa.JSON(), nullable=False),
        )
        op.create_index("ix_job_leases_job_id", "job_leases", ["job_id"])
        op.create_index("ix_job_leases_device_id", "job_leases", ["device_id"])
        op.create_index("ix_job_leases_status_expires", "job_leases", ["status", "expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("job_leases"):
        op.drop_table("job_leases")
    existing = {c["name"] for c in insp.get_columns("jobs")}
    if "parent_job_id" in existing:
        op.drop_constraint("fk_jobs_parent_job_id", "jobs", type_="foreignkey")
    if "idempotency_key" in existing:
        op.drop_constraint("uq_jobs_idempotency_key", "jobs", type_="unique")
    for col in reversed(JOB_COLUMNS):
        if col.name in existing:
            op.drop_column("jobs", col.name)
