"""Opportunity registry API (spec §13, §57.3, §80).

Lists opportunities with a reference profitability estimate and opportunity
score so ranking is visible to admins (spec §81 DoD), lets admins disable a
single opportunity, run discovery, and enqueue jobs built by the adapter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import registry
from app.db.models.jobs_ledger import Opportunity
from app.db.session import get_db
from app.dependencies import require_roles
from app.services import opportunity_service as opps
from app.services import profitability as prof

router = APIRouter()
ADMIN = ("OWNER", "ADMIN", "OPERATIONS")


class ReasonRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class BuildJobsRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=500)
    payload_overrides: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    constraints: dict[str, Any] = Field(default_factory=dict)
    auto_schedule: bool | None = None


async def _dict(db: AsyncSession, opp: Opportunity, policy: prof.CostPolicy, enabled: set[str]) -> dict[str, Any]:
    terms = opps.terms_from(opp, opp.workload_type or "")
    est = prof.estimate_job(terms, prof.DeviceProfile(), policy)
    score = prof.score_opportunity(est, terms, prof.DeviceProfile())
    return opps.opportunity_dict(opp, est, score, adapter_enabled=opp.adapter_id in enabled)


@router.get("/")
async def list_opportunities(adapter_id: str | None = None, include_disabled: bool = True,
                             db: AsyncSession = Depends(get_db), _=Depends(require_roles(*ADMIN, "FINANCE"))):
    await opps.ensure_registry(db)
    q = select(Opportunity)
    if adapter_id:
        q = q.where(Opportunity.adapter_id == adapter_id)
    rows = (await db.execute(q.order_by(Opportunity.adapter_id, Opportunity.external_id))).scalars().all()
    policy = await opps.cost_policy(db)
    enabled = await opps.enabled_adapter_ids(db)
    out = [await _dict(db, o, policy, enabled) for o in rows]
    if not include_disabled:
        out = [o for o in out if o["status"] == "ACTIVE" and o["adapter_enabled"]]
    out.sort(key=lambda o: -(o.get("opportunity_score") or 0))
    await db.commit()
    return {"opportunities": out, "count": len(out), "policy": policy.as_dict()}


@router.post("/discover")
async def discover(db: AsyncSession = Depends(get_db), _=Depends(require_roles(*ADMIN))):
    counts = await opps.discover_all(db)
    await db.commit()
    return {"discovered": counts, "at": datetime.now(timezone.utc).isoformat()}


async def _get(db: AsyncSession, opportunity_id: UUID) -> Opportunity:
    opp = await opps.get_opportunity(db, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return opp


@router.get("/{opportunity_id}")
async def get_opportunity(opportunity_id: UUID, db: AsyncSession = Depends(get_db),
                          _=Depends(require_roles(*ADMIN, "FINANCE"))):
    opp = await _get(db, opportunity_id)
    return await _dict(db, opp, await opps.cost_policy(db), await opps.enabled_adapter_ids(db))


@router.post("/{opportunity_id}/disable")
async def disable_opportunity(opportunity_id: UUID, req: ReasonRequest, db: AsyncSession = Depends(get_db),
                              _=Depends(require_roles(*ADMIN))):
    opp = await opps.set_opportunity_enabled(db, await _get(db, opportunity_id), False, req.reason)
    await db.commit()
    return {"opportunity_id": str(opp.id), "status": opp.status.value, "disabled_reason": opp.disabled_reason}


@router.post("/{opportunity_id}/enable")
async def enable_opportunity(opportunity_id: UUID, db: AsyncSession = Depends(get_db),
                             _=Depends(require_roles(*ADMIN))):
    opp = await opps.set_opportunity_enabled(db, await _get(db, opportunity_id), True, None)
    await db.commit()
    return {"opportunity_id": str(opp.id), "status": opp.status.value}


@router.post("/{opportunity_id}/jobs")
async def build_jobs(opportunity_id: UUID, req: BuildJobsRequest, db: AsyncSession = Depends(get_db),
                     current_user=Depends(require_roles(*ADMIN))):
    """Enqueue ``count`` jobs built by the opportunity's adapter. Each goes through
    the normal create path (kill switch + profitability gate + queue)."""
    from app.api import jobs as jobs_api

    opp = await _get(db, opportunity_id)
    adapter = registry.get(opp.adapter_id)
    if adapter is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown adapter")
    created = []
    for _ in range(req.count):
        try:
            spec = await adapter.build_job(opp, req.payload_overrides)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"build_job failed: {exc}")
        created.append(await jobs_api.create_job(
            jobs_api.JobCreateRequest(workload_type=spec.workload_type, payload=spec.payload,
                                      max_runtime_seconds=spec.max_runtime_seconds, priority=req.priority,
                                      opportunity_id=opp.id, constraints={**spec.constraints, **req.constraints},
                                      auto_schedule=req.auto_schedule),
            db=db, current_user=current_user, idempotency_key=None))
    return {"opportunity_id": str(opp.id), "created": created, "count": len(created),
            "rejected": sum(1 for c in created if c.get("rejected_reason"))}
