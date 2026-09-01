"""Seed the database with an initial owner/admin account and system settings."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import settings  # noqa: E402
from app.db.models.core import User, UserRole, Role, Provider, Farm, SystemSettings  # noqa: E402
from app.db.models.enums import RoleName, ProviderStatus, UserStatus  # noqa: E402
from app.security.signatures import hash_password  # noqa: E402
from app.db.session import AsyncSessionLocal, init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as session:
        # Ensure roles exist
        role_map: dict[RoleName, Role] = {}
        for role_name in RoleName:
            existing_role = await session.execute(
                select(Role).where(Role.name == role_name)
            )
            role = existing_role.scalar_one_or_none()
            if not role:
                role = Role(
                    name=role_name,
                    description=f"FARMOS role: {role_name.value}",
                )
                session.add(role)
                await session.flush()
            role_map[role_name] = role

        # Create owner/admin user if not exists
        result = await session.execute(
            select(User).where(User.email == settings.ADMIN_EMAIL)
        )
        admin = result.scalar_one_or_none()
        if not admin:
            admin = User(
                email=settings.ADMIN_EMAIL,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                full_name=settings.ADMIN_FULL_NAME,
                status=UserStatus.ACTIVE,
                is_superuser=True,
            )
            session.add(admin)
            await session.flush()

            # Assign all roles to owner
            for role_name in RoleName:
                session.add(
                    UserRole(user_id=admin.id, role_id=role_map[role_name].id)
                )

            # Create default provider record
            provider = Provider(
                user_id=admin.id,
                display_name=settings.ADMIN_FULL_NAME,
                status=ProviderStatus.ACTIVE,
                payout_hold=False,
            )
            session.add(provider)
            await session.flush()

            # Create default farm
            farm = Farm(
                name="Default Farm",
                description="Default FARMOS farm for the owner.",
                owner_user_id=admin.id,
                policy={"wifi_only": True, "charging_only": True},
            )
            session.add(farm)
            await session.flush()

            # Seed system settings
            defaults = {
                "thermal.green_max": 38.0,
                "thermal.yellow_max": 42.0,
                "thermal.orange_max": 45.0,
                "scheduler.enabled": True,
                "payout.minimum_usdc": 5.0,
                "wallet.withdrawal_cooldown_hours": 24,
                "node.heartbeat_interval_seconds": 30,
                "node.offline_after_seconds": 180,
                "price.max_staleness_seconds": 300,
            }
            for key, value in defaults.items():
                existing_setting = await session.execute(
                    select(SystemSettings).where(SystemSettings.key == key)
                )
                if not existing_setting.scalar_one_or_none():
                    session.add(
                        SystemSettings(
                            key=key,
                            value={"value": value},
                            description=f"System setting: {key}",
                            updated_by=admin.id,
                        )
                    )

            await session.commit()
            print(f"Seeded admin user: {settings.ADMIN_EMAIL}")
        else:
            # Keep the configured bootstrap identity usable on every startup.
            # This also repairs databases seeded with an earlier default password.
            admin.password_hash = hash_password(settings.ADMIN_PASSWORD)
            admin.full_name = settings.ADMIN_FULL_NAME
            admin.status = UserStatus.ACTIVE
            admin.is_superuser = True

            assigned_roles = await session.execute(
                select(UserRole.role_id).where(UserRole.user_id == admin.id)
            )
            assigned_role_ids = set(assigned_roles.scalars())
            for role in role_map.values():
                if role.id not in assigned_role_ids:
                    session.add(UserRole(user_id=admin.id, role_id=role.id))

            await session.commit()
            print(f"Reconciled admin user: {settings.ADMIN_EMAIL}")


if __name__ == "__main__":
    asyncio.run(seed())
