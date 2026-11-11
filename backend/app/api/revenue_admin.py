"""Admin: adapter kill switch, scheduler, economics, profitability policy (spec §14, §16, §17, §57.4, §126)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import registry
from app.db.models.devices import Device
from app.db.models.enums import JobStatus
from app.db.models.jobs_ledger import DeviceEconomics, Job, JobEconomics, RevenueAdapterRecord
from app.db.session import get_db
from app.dependencies import require_roles
from app.services import economics_service as econ
from app.services import opportunity_service as opps
from app.services import scheduler_service as sched

router = APIRouter()
ADMIN = ("OWNER", "ADMIN", "OPERATIONS")
READ = ("OWNER", "ADMIN", "OPERATIONS", "FINANCE")


class ReasonRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class TickRequest(BaseModel):
    job_ids: list[UUID] | None = None
    dry_run: bool = False
    limit: int | None = Field(default=None, ge=1, le=1000)


class DeviceEconomicsUpdate(BaseModel):
    purchase_price_usd: float | None = Field(default=None, ge=0, le=100000)
    resale_value_usd: float | None = Field(default=None, ge=0, le=100000)
    maintenance_usd: float | None = Field(default=None, ge=0, le=100000)
    active_watts: float | None = Field(default=None, ge=0, le=100)


def _adapter_dict(rec: RevenueAdapterRecord) -> dict[str, Any]:
    return {
        "adapter_id": rec.id, "name": rec.name, "category": rec.category, "kind": rec.kind,
        "simulated": rec.simulated, "enabled": rec.enabled, "disabled_reason": rec.disabled_reason,
        "disabled_at": rec.disabled_at.isoformat() if rec.disabled_at else None,
        "certification_stage": rec.certification_stage, "health_status": rec.health_status,
        "health_detail": rec.health_detail or {}, "last_reconcile": rec.last_reconcile or {},
        "last_discovery_at": rec.last_discovery_at.isoformat() if rec.last_discovery_at else None,
    }


async def _rec(db: AsyncSession, adapter_id: str) -> RevenueAdapterRecord:
    await opps.ensure_registry(db, discover=False)
    rec = await opps.adapter_record(db, adapter_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Adapter not found")
    return rec


# ── adapters / kill switch ───────────────────────────────────────────────────

@router.get("/adapters")
async def list_adapters(db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    await opps.ensure_registry(db)
    rows = (await db.execute(select(RevenueAdapterRecord).order_by(RevenueAdapterRecord.id))).scalars().all()
    out = []
    for r in rows:
        d = _adapter_dict(r)
        a = registry.get(r.id)
        d["constraints"] = await a.get_constraints() if a else {}
        out.append(d)
    await db.commit()
    return {"adapters": out, "system_adapters": sorted(registry.SYSTEM_ADAPTER_IDS)}


@router.post("/adapters/{adapter_id}/disable")
async def disable_adapter(adapter_id: str, req: ReasonRequest, db: AsyncSession = Depends(get_db),
                          current_user=Depends(require_roles(*ADMIN))):
    await _rec(db, adapter_id)
    rec = await opps.set_adapter_enabled(db, adapter_id, False, req.reason, current_user.id)
    await db.commit()
    return _adapter_dict(rec)


@router.post("/adapters/{adapter_id}/enable")
async def enable_adapter(adapter_id: str, db: AsyncSession = Depends(get_db),
                         current_user=Depends(require_roles(*ADMIN))):
    await _rec(db, adapter_id)
    rec = await opps.set_adapter_enabled(db, adapter_id, True, None, current_user.id)
    await db.commit()
    return _adapter_dict(rec)


@router.post("/adapters/{adapter_id}/health")
async def adapter_health(adapter_id: str, db: AsyncSession = Depends(get_db), _=Depends(require_roles(*ADMIN))):
    await _rec(db, adapter_id)
    h = await opps.health_check(db, adapter_id)
    await db.commit()
    return h


@router.post("/adapters/{adapter_id}/reconcile")
async def adapter_reconcile(adapter_id: str, db: AsyncSession = Depends(get_db),
                            current_user=Depends(require_roles(*ADMIN, "FINANCE"))):
    await _rec(db, adapter_id)
    out = await opps.reconcile(db, adapter_id, current_user.id)
    await db.commit()
    return out


# ── scheduler ────────────────────────────────────────────────────────────────

@router.post("/scheduler/tick")
async def scheduler_tick(req: TickRequest, db: AsyncSession = Depends(get_db), _=Depends(require_roles(*ADMIN))):
    report = await sched.scheduler_tick(db, job_ids=req.job_ids, dry_run=req.dry_run, limit=req.limit)
    await db.commit()
    return report


@router.get("/scheduler/queue")
async def scheduler_queue(limit: int = Query(default=50, ge=1, le=500), db: AsyncSession = Depends(get_db),
                          _=Depends(require_roles(*READ))):
    rows = (await db.execute(select(Job).where(Job.status == JobStatus.QUEUED, Job.dead_lettered.is_(False))
                             .order_by(Job.priority.desc(), Job.created_at).limit(limit))).scalars().all()
    return {"queued": [{"job_id": str(j.id), "workload_type": j.workload_type, "priority": j.priority,
                        "adapter_id": j.adapter_id, "retry_count": j.retry_count, "last_error": j.last_error,
                        "expected_net_usd": float(j.expected_profit_usd) if j.expected_profit_usd is not None else None,
                        "created_at": j.created_at.isoformat() if j.created_at else None} for j in rows],
            "count": len(rows)}


@router.get("/scheduler/preview/{job_id}")
async def scheduler_preview(job_id: UUID, db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return await sched.preview(db, job)


# ── economics ────────────────────────────────────────────────────────────────

@router.get("/economics/jobs")
async def economics_jobs(status_filter: str | None = Query(default=None, alias="status"),
                         limit: int = Query(default=100, ge=1, le=1000), db: AsyncSession = Depends(get_db),
                         _=Depends(require_roles(*READ))):
    q = select(JobEconomics)
    if status_filter:
        q = q.where(JobEconomics.status == status_filter.upper())
    rows = (await db.execute(q.order_by(JobEconomics.estimated_at.desc()).limit(limit))).scalars().all()
    return {"jobs": [econ.job_economics_dict(r) for r in rows], "count": len(rows)}


@router.get("/economics/jobs/{job_id}")
async def economics_job(job_id: UUID, db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    row = (await db.execute(select(JobEconomics).where(JobEconomics.job_id == job_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No economics for job")
    return econ.job_economics_dict(row)


@router.get("/economics/devices")
async def economics_devices(limit: int = Query(default=200, ge=1, le=2000), db: AsyncSession = Depends(get_db),
                            _=Depends(require_roles(*READ))):
    devices = (await db.execute(select(Device).order_by(Device.human_id).limit(limit))).scalars().all()
    des = await econ.device_economics_rows(db, [d.id for d in devices])
    policy = await opps.cost_policy(db)
    rows = [econ.device_pnl_dict(d, des.get(d.id), policy) for d in devices]
    rows.sort(key=lambda r: -r["net_usd"])
    return {"devices": rows, "count": len(rows)}


@router.put("/devices/{device_id}/economics")
async def update_device_economics(device_id: UUID, req: DeviceEconomicsUpdate, db: AsyncSession = Depends(get_db),
                                  _=Depends(require_roles("OWNER", "ADMIN", "FINANCE"))):
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    de = (await db.execute(select(DeviceEconomics).where(DeviceEconomics.device_id == device_id))).scalar_one_or_none()
    if de is None:
        de = DeviceEconomics(device_id=device_id)
        db.add(de)
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(de, k, v)
    await db.flush()
    await db.refresh(de)
    out = econ.device_pnl_dict(device, de, await opps.cost_policy(db))
    await db.commit()
    return out


@router.get("/economics/summary")
async def economics_summary(hours: int = Query(default=24, ge=1, le=24 * 365), db: AsyncSession = Depends(get_db),
                            _=Depends(require_roles(*READ))):
    return await econ.summary(db, hours)


# ── profitability policy ─────────────────────────────────────────────────────

@router.get("/profitability/policy")
async def get_policy(db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    return {"effective": (await opps.cost_policy(db)).as_dict(), "override": await opps.get_policy_override(db)}


@router.put("/profitability/policy")
async def put_policy(value: dict[str, Any], db: AsyncSession = Depends(get_db),
                     current_user=Depends(require_roles("OWNER", "ADMIN", "FINANCE"))):
    try:
        policy = await opps.set_policy_override(db, value, current_user.id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    await db.commit()
    return {"effective": policy.as_dict(), "override": value}
