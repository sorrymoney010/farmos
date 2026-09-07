"""Add Acurast processor management records.

Revision ID: 0002_acurast_mgmt
Revises: 0001_initial_schema
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0002_acurast_mgmt"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # FARMOS calls metadata.create_all during local startup. Make the migration
    # safe if a development process created this table before Alembic advances
    # its version marker; fresh/staging databases still take the normal DDL path.
    if sa.inspect(bind).has_table("acurast_processors"):
        return

    op.create_table(
        "acurast_processors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("address", sa.String(100), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("farmos_device_id", UUID(as_uuid=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("platform", sa.Integer(), nullable=True),
        sa.Column("last_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("battery_level", sa.Float(), nullable=True),
        sa.Column("is_charging", sa.Boolean(), nullable=True),
        sa.Column("battery_health", sa.String(50), nullable=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("network_type", sa.String(50), nullable=True),
        sa.Column("ssid", sa.String(255), nullable=True),
        sa.Column("latest_check_in", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["farmos_device_id"], ["devices.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("address", name="uq_acurast_processors_address"),
    )
    op.create_index("ix_acurast_processors_farmos_device_id", "acurast_processors", ["farmos_device_id"])
    op.create_index("ix_acurast_processors_enabled", "acurast_processors", ["enabled"])
    op.create_index("ix_acurast_processors_last_reported_at", "acurast_processors", ["last_reported_at"])


def downgrade() -> None:
    op.drop_index("ix_acurast_processors_last_reported_at", table_name="acurast_processors")
    op.drop_index("ix_acurast_processors_enabled", table_name="acurast_processors")
    op.drop_index("ix_acurast_processors_farmos_device_id", table_name="acurast_processors")
    op.drop_table("acurast_processors")
