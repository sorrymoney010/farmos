"""Jobs endpoints for FARMOS."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.jobs_ledger import Job, JobAssignment, JobEvent
from app.db.models.enums import JobStatus, JobEventActorType
from app.db.session import get_db
from app.dependencies import require_roles, get_current_user
from app.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


class JobCreateRequest(BaseModel):
    workload_type: str
    requested_resources: dict[str, Any]
    expected_revenue_usd: float | None = None
    expected_cost_usd: float | None = None
    max_runtime_seconds: int | None = None
    priority: int = 100


@router.post("/")
async def create_job(
    req: JobCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "CUSTOMER")),
):
    job = Job(
        workload_type=req.workload_type,
        status=JobStatus.CREATED,
        priority=req.priority,
        requested_resources=req.requested_resources,
        expected_revenue_usd=req.expected_revenue_usd,
        expected_cost_usd=req.expected_cost_usd,
        adapter_id="internal",
    )
    db.add(job)
    await db.flush()
    event = JobEvent(
        job_id=job.id,
        from_status=None,
        to_status=JobStatus.CREATED.value,
        actor_type=JobEventActorType.USER.value,
        actor_id=current_user.id,
    )
    db.add(event)
    await db.commit()
    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
    }


@router.get("/")
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "OPERATIONS", "CUSTOMER")),
):
    result = await db.execute(select(Job).order_by(Job.created_at.desc()).limit(100))
    jobs = result.scalars().all()
    return [
        {
            "job_id": str(j.id),
            "status": j.status.value,
            "workload_type": j.workload_type,
            "priority": j.priority,
            "created_at": j.created_at.isoformat(),
        }
        for j in jobs
    ]
