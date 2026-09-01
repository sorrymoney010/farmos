"""Initial FARMOS schema migration."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import text


revision = "0001_initial_schema"
down_revision = None
description = "Create core FARMOS tables"


def upgrade() -> None:
    op.execute(text("""
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    """))

    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), server_default="ACTIVE", nullable=False),
        sa.Column("is_superuser", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "roles",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    op.create_table(
        "user_roles",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])

    op.create_table(
        "providers",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(30), server_default="PENDING_VERIFICATION", nullable=False),
        sa.Column("payout_hold", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_providers_user_id", "providers", ["user_id"])

    op.create_table(
        "farms",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("policy", sa.JSON, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "farm_memberships",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("farm_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", UUID(as_uuid=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("farm_id", "provider_id", name="uq_farm_provider"),
    )

    op.create_table(
        "devices",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("provider_id", UUID(as_uuid=True), nullable=False),
        sa.Column("farm_id", UUID(as_uuid=True), nullable=True),
        sa.Column("human_id", sa.String(100), nullable=False),
        sa.Column("public_key", sa.Text, nullable=False),
        sa.Column("hardware_fingerprint", sa.String(255), nullable=True),
        sa.Column("manufacturer", sa.String(100), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("android_version", sa.String(50), nullable=True),
        sa.Column("architecture", sa.String(50), nullable=True),
        sa.Column("cpu_cores", sa.Integer, nullable=True),
        sa.Column("ram_mb", sa.Integer, nullable=True),
        sa.Column("storage_total_mb", sa.BigInteger, nullable=True),
        sa.Column("status", sa.String(30), server_default="ENROLLING", nullable=False),
        sa.Column("farm_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("reputation_score", sa.Numeric(8, 4), server_default="50", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("human_id", name="uq_devices_human_id"),
        sa.UniqueConstraint("public_key", name="uq_devices_public_key"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_devices_provider_id", "devices", ["provider_id"])
    op.create_index("ix_devices_farm_id", "devices", ["farm_id"])
    op.create_index("ix_devices_status", "devices", ["status"])

    op.create_table(
        "device_heartbeats",
        sa.Column("id", sa.BigInteger, autoincrement=True, nullable=False),
        sa.Column("device_id", UUID(as_uuid=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("battery_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("charging", sa.Boolean, nullable=True),
        sa.Column("temperature_c", sa.Numeric(5, 2), nullable=True),
        sa.Column("cpu_util_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("ram_used_mb", sa.Integer, nullable=True),
        sa.Column("storage_free_mb", sa.BigInteger, nullable=True),
        sa.Column("network_type", sa.String(50), nullable=True),
        sa.Column("down_mbps", sa.Numeric(10, 2), nullable=True),
        sa.Column("up_mbps", sa.Numeric(10, 2), nullable=True),
        sa.Column("app_version", sa.String(50), nullable=True),
        sa.Column("metadata", sa.JSON, server_default="{}", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_heartbeats_device_time", "device_heartbeats", ["device_id", "observed_at"])

    op.create_table(
        "device_benchmarks",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("device_id", UUID(as_uuid=True), nullable=False),
        sa.Column("suite_version", sa.String(50), nullable=False),
        sa.Column("cpu_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("ai_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("memory_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("storage_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("network_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("thermal_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("power_efficiency_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("reliability_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("farm_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("raw_results", sa.JSON, server_default="{}", nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_benchmarks_device_id", "device_benchmarks", ["device_id"])

    op.create_table(
        "device_capabilities",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("device_id", UUID(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("device_id", "capability", name="uq_device_capability"),
    )

    op.create_table(
        "device_health_events",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("device_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("metadata", sa.JSON, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_health_events_device_id", "device_health_events", ["device_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger, autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("actor_type", sa.String(50), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=True),
        sa.Column("target_type", sa.String(50), nullable=True),
        sa.Column("target_id", UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("ip_hash", sa.String(64), nullable=True),
        sa.Column("metadata", sa.JSON, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    op.create_table(
        "system_settings",
        sa.Column("id", UUID(as_uuid=True), server_default=text("uuid_generate_v4()"), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.JSON, server_default="{}", nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_system_settings_key"),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
    op.drop_table("audit_logs")
    op.drop_table("device_health_events")
    op.drop_table("device_capabilities")
    op.drop_table("device_benchmarks")
    op.drop_table("device_heartbeats")
    op.drop_table("devices")
    op.drop_table("farm_memberships")
    op.drop_table("farms")
    op.drop_table("providers")
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("users")
