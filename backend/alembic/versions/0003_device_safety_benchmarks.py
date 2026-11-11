"""Sprint 3: device health/safety state columns (benchmarks, FarmScore, thermal).

Revision ID: 0003_device_safety
Revises: 0002_acurast_mgmt

device_benchmarks already exists (0001). This adds the per-device safety state
used by the thermal/battery governor (spec §10, §60, §62). Idempotent so it is
safe on databases that were bootstrapped with metadata.create_all.
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_device_safety"
down_revision = "0002_acurast_mgmt"
branch_labels = None
depends_on = None


COLUMNS = [
    sa.Column("temperature_state", sa.String(20), nullable=True),
    sa.Column("safety_paused", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("safety_reason", sa.String(255), nullable=True),
    sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_temperature_c", sa.Numeric(5, 2), nullable=True),
    sa.Column("last_battery_pct", sa.Numeric(5, 2), nullable=True),
    sa.Column("last_charging", sa.Boolean(), nullable=True),
    sa.Column("battery_health", sa.String(50), nullable=True),
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("devices")}
    for col in COLUMNS:
        if col.name not in existing:
            op.add_column("devices", col.copy())


def downgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("devices")}
    for col in reversed(COLUMNS):
        if col.name in existing:
            op.drop_column("devices", col.name)
