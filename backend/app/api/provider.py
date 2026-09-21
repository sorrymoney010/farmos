"""Provider endpoints for FARMOS."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.core import Provider
from app.db.models.devices import Device
from app.db.models.jobs_ledger import Job, LedgerEntry
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
    """V1 dashboard: devices (status/last seen), jobs summary, earnings/ledger."""
    result = await db.execute(select(Provider).where(Provider.user_id == current_user.id))
    provider = result.scalar_one_or_none()
    if not provider:
        return {
            "online_devices": 0,
            "devices": [],
            "jobs": {"pending": 0, "running": 0, "complete": 0, "recent": []},
            "earnings": {"total_credits": 0.0, "total_debits": 0.0, "net": 0.0},
        }

    devices_result = await db.execute(select(Device).where(Device.provider_id == provider.id))
    devices = devices_result.scalars().all()

    jobs_result = await db.execute(select(Job).order_by(Job.created_at.desc()).limit(50))
    jobs = jobs_result.scalars().all()
    pending_statuses = {"CREATED", "QUEUED", "DISPATCHED"}
    running_statuses = {"ACCEPTED", "RUNNING", "RESULT_SUBMITTED", "VERIFYING"}
    complete_statuses = {"COMPLETE", "COMPLETED", "VERIFIED", "PAID"}
    pending = running = complete = 0
    recent = []
    for j in jobs:
        st = j.status.value if hasattr(j.status, "value") else str(j.status)
        if st in pending_statuses:
            pending += 1
        elif st in running_statuses:
            running += 1
        elif st in complete_statuses:
            complete += 1
        if len(recent) < 20:
            recent.append(
                {
                    "job_id": str(j.id),
                    "status": st,
                    "workload_type": j.workload_type,
                    "created_at": j.created_at.isoformat() if j.created_at else None,
                }
            )

    credit_q = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(LedgerEntry.direction == "CREDIT")
    )
    debit_q = await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(LedgerEntry.direction == "DEBIT")
    )
    total_credits = float(credit_q.scalar() or 0)
    total_debits = float(debit_q.scalar() or 0)

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
                "farm_score": float(d.farm_score) if d.farm_score is not None else None,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            }
            for d in devices
        ],
        "jobs": {
            "pending": pending,
            "running": running,
            "complete": complete,
            "recent": recent,
        },
        "earnings": {
            "total_credits": total_credits,
            "total_debits": total_debits,
            "net": total_credits - total_debits,
        },
    }