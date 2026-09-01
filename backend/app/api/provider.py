"""Provider endpoints for FARMOS."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.core import Provider
from app.db.models.devices import Device
from app.db.session import get_db
from app.dependencies import get_current_user, require_roles
from app.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.get("/dashboard")
async def provider_dashboard(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "PROVIDER", "OPERATIONS")),
):
    result = await db.execute(select(Provider).where(Provider.user_id == current_user.id))
    provider = result.scalar_one_or_none()
    if not provider:
        return {"online_devices": 0, "devices": [], "earnings": {}}
    devices_result = await db.execute(select(Device).where(Device.provider_id == provider.id))
    devices = devices_result.scalars().all()
    return {
        "provider_id": str(provider.id),
        "display_name": provider.display_name,
        "status": provider.status.value,
        "online_devices": len([d for d in devices if d.status.value in ("ACTIVE", "RUNNING", "IDLE")]),
        "devices": [
            {
                "device_id": str(d.id),
                "human_id": d.human_id,
                "status": d.status.value,
                "farm_score": d.farm_score,
            }
            for d in devices
        ],
    }
