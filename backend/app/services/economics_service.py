"""Per-job and per-device economics (spec §18, §57.5, §64, §65).

* ``record_estimate`` stores the match-time estimate (and the scoring inputs)
  for every scheduled, dispatched or rejected job.
* ``finalize_job`` replaces estimates with actuals once a PRIMARY job is
  verified and rolls them into the device P&L exactly once.

Revenue states (no money moves in Phase 3):
  UNCONFIRMED  internal customer work — billed/confirmed by Phase 4 ledger
  SIMULATED    simulated external adapter — never real money
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.devices import Device, DeviceHeartbeat
from app.db.models.jobs_ledger import DeviceEconomics, Job, JobEconomics, JobLease, JobResult
from app.logging import get_logger
from app.services import opportunity_service as opps
from app.services import profitability as prof

logger = get_logger(__name__)


def _f(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


async def latest_heartbeats(db: AsyncSession, device_ids: list) -> dict:
    if not device_ids:
        return {}
    rows = (await db.execute(
        select(DeviceHeartbeat).where(DeviceHeartbeat.device_id.in_(device_ids))
        .distinct(DeviceHeartbeat.device_id)
        .order_by(DeviceHeartbeat.device_id, DeviceHeartbeat.observed_at.desc())
    )).scalars().all()
    return {r.device_id: r for r in rows}


async def device_economics_rows(db: AsyncSession, device_ids: list) -> dict:
    if not device_ids:
        return {}
    rows = (await db.execute(select(DeviceEconomics).where(DeviceEconomics.device_id.in_(device_ids)))).scalars().all()
    return {r.device_id: r for r in rows}


def device_profile(device: Device, hb: Optional[DeviceHeartbeat] = None,
                   de: Optional[DeviceEconomics] = None) -> prof.DeviceProfile:
    avail_cpu = (100.0 - float(hb.cpu_util_pct)) if hb is not None and hb.cpu_util_pct is not None else None
    avail_ram = None
    if hb is not None and hb.ram_used_mb is not None and device.ram_mb:
        avail_ram = max(float(device.ram_mb) - float(hb.ram_used_mb), 0.0)
    bw = float(hb.down_mbps) if hb is not None and hb.down_mbps is not None else None
    return prof.DeviceProfile(
        device_id=str(device.id),
        farm_score=_f(device.farm_score),
        reputation=float(device.reputation_score or 0),
        purchase_price_usd=_f(de.purchase_price_usd) if de else None,
        resale_value_usd=_f(de.resale_value_usd) if de else None,
        active_watts=_f(de.active_watts) if de else None,
        temperature_state=(device.temperature_state or "UNKNOWN"),
        charging=device.last_charging,
        available_cpu_pct=avail_cpu,
        available_ram_mb=avail_ram,
        available_bandwidth_mbps=bw,
    )


async def record_estimate(db: AsyncSession, job: Job, device_id, est: prof.RevenueEstimate, *,
                          status: str = "ESTIMATED", opportunity_score: float = 0.0,
                          fit: Optional[float] = None, final: Optional[float] = None,
                          inputs: dict[str, Any] | None = None) -> JobEconomics:
    row = (await db.execute(select(JobEconomics).where(JobEconomics.job_id == job.id))).scalar_one_or_none()
    if row is None:
        row = JobEconomics(job_id=job.id)
        db.add(row)
    if row.status == "ACTUAL":
        return row
    row.opportunity_id = job.opportunity_id
    row.adapter_id = job.adapter_id
    row.device_id = device_id
    row.status = status
    row.asset_symbol = est.asset_symbol
    row.expected_gross_usd = est.gross_usd
    row.expected_adjusted_gross_usd = est.adjusted_gross_usd
    row.expected_cost_usd = est.total_cost_usd
    row.expected_net_usd = est.expected_net_profit_usd
    row.expected_costs = {k: round(v, 10) for k, v in est.costs.items()}
    row.expected_runtime_seconds = est.runtime_seconds
    row.opportunity_score = opportunity_score
    row.device_fit = fit
    row.final_score = final
    row.score_inputs = {**(inputs or {}), "failure_probability": est.failure_probability,
                        "volatility_haircut": est.volatility_haircut, "liquidity_haircut": est.liquidity_haircut}
    row.estimated_at = datetime.now(timezone.utc)
    job.expected_revenue_usd = est.adjusted_gross_usd
    job.expected_cost_usd = est.total_cost_usd
    job.expected_profit_usd = est.expected_net_profit_usd
    return row


async def finalize_job(db: AsyncSession, job: Job, result: JobResult, now: datetime | None = None) -> Optional[dict]:
    """Spec §65 actual job economics. Idempotent (only the first call posts)."""
    now = now or datetime.now(timezone.utc)
    row = (await db.execute(select(JobEconomics).where(JobEconomics.job_id == job.id))).scalar_one_or_none()
    if row is not None and row.status == "ACTUAL":
        return None
    device = (await db.execute(select(Device).where(Device.id == result.device_id))).scalar_one_or_none()
    if device is None:
        return None
    de = (await db.execute(select(DeviceEconomics).where(DeviceEconomics.device_id == device.id))).scalar_one_or_none()
    if de is None:
        de = DeviceEconomics(device_id=device.id, maintenance_usd=0, lifetime_gross_usd=0, electricity_usd=0,
                             network_usd=0, infrastructure_usd=0, depreciation_usd=0, other_costs_usd=0,
                             net_usd=0, jobs_completed=0, runtime_seconds=0)
        db.add(de)
        await db.flush()

    opp = await opps.get_opportunity(db, job.opportunity_id) if job.opportunity_id else None
    terms = opps.terms_from(opp, job.workload_type, job.constraints)
    policy = await opps.cost_policy(db)
    profile = device_profile(device, None, de)

    lease = (await db.execute(select(JobLease).where(JobLease.job_id == job.id, JobLease.device_id == device.id)
                              .order_by(JobLease.attempt.desc()).limit(1))).scalars().first()
    runtime_s = None
    if lease is not None and lease.accepted_at is not None:
        end = lease.closed_at or result.submitted_at or now
        runtime_s = max((end - lease.accepted_at).total_seconds(), 0.0)
    if runtime_s is None:
        runtime_s = float(row.expected_runtime_seconds) if row is not None else prof.estimate_runtime_seconds(terms, profile)
    metrics = {}
    try:
        import json as _json
        metrics = _json.loads(result.result_uri) if result.result_uri else {}
    except ValueError:
        metrics = {}
    net_bytes = float(metrics.get("bytes_transferred") or 0) or float(terms.expected_network_bytes or 0)

    # Gross: customer price (USD) or token reward valued at the snapshot with haircuts.
    if terms.reward_asset_per_job is None and row is not None and float(row.expected_gross_usd or 0) > 0:
        gross = adjusted = float(row.expected_gross_usd)  # customer-quoted USD price
    else:
        # Token rewards are re-valued at the current snapshot, with haircuts.
        gross = prof.gross_usd(terms, runtime_s, policy)
        vol, liq = prof.haircuts(terms.asset_symbol, policy)
        adjusted = gross * (1 - vol) * (1 - liq)
    costs = prof.actual_costs(runtime_s=runtime_s, gross=gross, network_bytes=net_bytes, device=profile,
                              terms=terms, p=policy)
    total = sum(costs.values())
    net = adjusted - total
    simulated = bool((opp.constraints or {}).get("simulated")) if opp else False

    if row is None:
        row = JobEconomics(job_id=job.id, opportunity_id=job.opportunity_id, adapter_id=job.adapter_id,
                           expected_costs={}, score_inputs={}, asset_symbol=terms.asset_symbol)
        db.add(row)
    row.device_id = device.id
    row.status = "ACTUAL"
    row.actual_runtime_seconds = runtime_s
    row.actual_gross_usd = adjusted
    row.actual_cost_usd = total
    row.actual_net_usd = net
    row.actual_costs = {k: round(v, 10) for k, v in costs.items()}
    row.revenue_state = "SIMULATED" if simulated else "UNCONFIRMED"
    row.finalized_at = now

    de.lifetime_gross_usd = float(de.lifetime_gross_usd or 0) + adjusted
    de.electricity_usd = float(de.electricity_usd or 0) + costs["electricity"]
    de.network_usd = float(de.network_usd or 0) + costs["network"]
    de.infrastructure_usd = float(de.infrastructure_usd or 0) + costs["infrastructure"]
    de.depreciation_usd = float(de.depreciation_usd or 0) + costs["depreciation"]
    de.other_costs_usd = float(de.other_costs_usd or 0) + costs["api"] + costs["chain_fee"] + costs["payout_fee"]
    de.net_usd = float(de.net_usd or 0) + net
    de.jobs_completed = int(de.jobs_completed or 0) + 1
    de.runtime_seconds = float(de.runtime_seconds or 0) + runtime_s
    de.first_job_at = de.first_job_at or now
    de.last_job_at = now
    logger.info("job_economics_finalized", job_id=str(job.id), net_usd=round(net, 8), state=row.revenue_state)
    return {"actual_gross_usd": adjusted, "actual_cost_usd": total, "actual_net_usd": net,
            "revenue_state": row.revenue_state}


def job_economics_dict(row: JobEconomics) -> dict[str, Any]:
    return {
        "job_id": str(row.job_id), "device_id": str(row.device_id) if row.device_id else None,
        "adapter_id": row.adapter_id, "opportunity_id": str(row.opportunity_id) if row.opportunity_id else None,
        "status": row.status, "asset_symbol": row.asset_symbol, "revenue_state": row.revenue_state,
        "expected": {"gross_usd": _f(row.expected_gross_usd), "adjusted_gross_usd": _f(row.expected_adjusted_gross_usd),
                     "cost_usd": _f(row.expected_cost_usd), "net_usd": _f(row.expected_net_usd),
                     "costs": row.expected_costs or {}, "runtime_seconds": _f(row.expected_runtime_seconds)},
        "scores": {"opportunity_score": _f(row.opportunity_score), "device_fit": _f(row.device_fit),
                   "final_score": _f(row.final_score), "inputs": row.score_inputs or {}},
        "actual": None if row.status != "ACTUAL" else {
            "gross_usd": _f(row.actual_gross_usd), "cost_usd": _f(row.actual_cost_usd),
            "net_usd": _f(row.actual_net_usd), "costs": row.actual_costs or {},
            "runtime_seconds": _f(row.actual_runtime_seconds)},
        "estimated_at": row.estimated_at.isoformat() if row.estimated_at else None,
        "finalized_at": row.finalized_at.isoformat() if row.finalized_at else None,
    }


def device_pnl_dict(device: Device, de: Optional[DeviceEconomics], policy: prof.CostPolicy,
                    now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    purchase = float(de.purchase_price_usd) if de and de.purchase_price_usd is not None else policy.default_purchase_usd
    resale = float(de.resale_value_usd) if de and de.resale_value_usd is not None else policy.default_resale_usd
    gross = float(de.lifetime_gross_usd) if de else 0.0
    net = float(de.net_usd) - float(de.maintenance_usd or 0) if de else 0.0
    runtime = float(de.runtime_seconds) if de else 0.0
    days = max((now - de.first_job_at).total_seconds() / 86400.0, 1.0 / 24) if de and de.first_job_at else 0.0
    return {
        "device_id": str(device.id), "human_id": device.human_id, "model": device.model,
        "status": device.status.value, "farm_score": _f(device.farm_score),
        "reputation_score": _f(device.reputation_score),
        "purchase_price_usd": purchase, "resale_value_usd": resale,
        "purchase_price_is_default": not (de and de.purchase_price_usd is not None),
        "lifetime_gross_usd": gross,
        "electricity_usd": float(de.electricity_usd) if de else 0.0,
        "network_usd": float(de.network_usd) if de else 0.0,
        "infrastructure_usd": float(de.infrastructure_usd) if de else 0.0,
        "depreciation_usd": float(de.depreciation_usd) if de else 0.0,
        "other_costs_usd": float(de.other_costs_usd) if de else 0.0,
        "maintenance_usd": float(de.maintenance_usd) if de else 0.0,
        "net_usd": net, "jobs_completed": int(de.jobs_completed) if de else 0,
        "runtime_seconds": runtime,
        **prof.device_roi(purchase_price=purchase, net_usd=net, gross_usd=gross, runtime_seconds=runtime,
                          days_active=days),
    }


async def summary(db: AsyncSession, since_hours: int = 24) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    q = select(JobEconomics.revenue_state, func.count(), func.coalesce(func.sum(JobEconomics.actual_gross_usd), 0),
               func.coalesce(func.sum(JobEconomics.actual_cost_usd), 0), func.coalesce(func.sum(JobEconomics.actual_net_usd), 0)) \
        .where(JobEconomics.status == "ACTUAL", JobEconomics.finalized_at >= since) \
        .group_by(JobEconomics.revenue_state)
    by_state = {r[0]: {"jobs": int(r[1]), "gross_usd": float(r[2]), "cost_usd": float(r[3]), "net_usd": float(r[4])}
                for r in (await db.execute(q)).all()}
    rejected = (await db.execute(select(func.count()).select_from(JobEconomics)
                                 .where(JobEconomics.status == "REJECTED", JobEconomics.estimated_at >= since))).scalar()
    by_adapter = {r[0]: {"jobs": int(r[1]), "net_usd": float(r[2])} for r in (await db.execute(
        select(JobEconomics.adapter_id, func.count(), func.coalesce(func.sum(JobEconomics.actual_net_usd), 0))
        .where(JobEconomics.status == "ACTUAL", JobEconomics.finalized_at >= since)
        .group_by(JobEconomics.adapter_id))).all()}
    return {"window_hours": since_hours, "by_revenue_state": by_state, "by_adapter": by_adapter,
            "rejected_unprofitable": int(rejected or 0),
            "note": "UNCONFIRMED = awaiting customer billing (Phase 4); SIMULATED = never real money"}
