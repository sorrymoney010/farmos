"""Deterministic, auditable scheduler (spec §16, §57.4, §62, §63, §146-148).

scheduler_tick():
  1. Load QUEUED jobs ordered by effective priority (priority + aging, spec
     anti-starvation) then created_at.
  2. Hard exclusions first (never overridden by profit or urgency):
     adapter kill switch / opportunity disabled or full, offline, locked or
     onboarding status, cooldown, RED/COOLING, safety policy band/battery
     (Phase 1 governor), busy (active lease = capacity reserved), excluded
     after a failed attempt (Phase 2), device affinity, min FarmScore,
     capability fit (RAM), per-provider and per-workload caps.
  3. Economics per remaining device (profitability engine); net <= 0 excluded.
  4. score = opportunity_score x device_fit x reputation x urgency x tier.
     Ties: higher FarmScore, then device id (deterministic).
  5. Transactionally lock device + job, re-check (job still QUEUED, device
     still has capacity), assign + issue lease: QUEUED -> MATCHED -> DISPATCHED.
     Never double-reserves capacity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.devices import Device
from app.db.models.enums import JobStatus
from app.db.models.jobs_ledger import Job, JobAssignment, JobLease
from app.logging import get_logger
from app.services import benchmark_service as bench_svc
from app.services import economics_service as econ
from app.services import lease_service as leases
from app.services import opportunity_service as opps
from app.services import profitability as prof
from app.services.device_safety import SCHEDULABLE_STATUSES, dispatch_block_reason

logger = get_logger(__name__)

INFLIGHT = (JobStatus.MATCHED, JobStatus.DISPATCHED, JobStatus.ACCEPTED, JobStatus.RUNNING)
SYSTEM_WORKLOADS = {"benchmark"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Candidate:
    device: Any
    profile: prof.DeviceProfile
    busy: int = 0
    block_reason: Callable[[str], Optional[str]] = lambda wt: None


@dataclass
class JobContext:
    job_id: Any
    workload_type: str
    priority: int
    created_at: datetime
    terms: prof.OpportunityTerms
    deadline_at: Optional[datetime] = None
    constraints: dict[str, Any] = field(default_factory=dict)
    excluded: set[str] = field(default_factory=set)
    gross_override: Optional[float] = None
    max_runtime: Optional[int] = None
    retry_count: int = 0
    system: bool = False


@dataclass
class Caps:
    provider_inflight: dict[str, int] = field(default_factory=dict)
    workload_inflight: dict[str, int] = field(default_factory=dict)
    max_per_device: int = 1
    max_per_provider: int = 500
    workload_caps: dict[str, int] = field(default_factory=dict)


class _Conflict(Exception):
    pass


def effective_priority(priority: int, created_at: datetime, now: datetime) -> float:
    age_min = max((now - created_at).total_seconds() / 60.0, 0.0)
    return priority + min(age_min * settings.SCHEDULER_AGING_PER_MINUTE, settings.SCHEDULER_AGING_MAX)


def hard_exclusion(ctx: JobContext, cand: Candidate, caps: Caps, now: datetime) -> Optional[str]:
    d = cand.device
    did = str(d.id)
    if did in ctx.excluded:
        return "excluded after failed attempt"
    affinity = ctx.constraints.get("device_affinity")
    if affinity and did not in {str(a) for a in affinity}:
        return "not in device affinity"
    if d.last_seen_at is None or (now - d.last_seen_at).total_seconds() > settings.DEVICE_ONLINE_SECONDS:
        return "offline"
    if (d.temperature_state or "").upper() in ("RED", "COOLING"):
        return f"thermal {d.temperature_state}"
    blocked = cand.block_reason(ctx.workload_type)
    if blocked:
        return blocked
    if d.status not in SCHEDULABLE_STATUSES:
        return f"status {d.status.value}"
    if cand.busy >= caps.max_per_device:
        return "busy (capacity reserved)"
    min_fs = ctx.constraints.get("min_farm_score", ctx.terms.min_farm_score)
    if min_fs is not None and (d.farm_score is None or float(d.farm_score) < float(min_fs)):
        return f"FarmScore below {float(min_fs):g}"
    need_ram = ctx.constraints.get("ram_mb")
    if need_ram and d.ram_mb and float(d.ram_mb) < float(need_ram):
        return "insufficient RAM"
    if caps.provider_inflight.get(str(d.provider_id), 0) >= caps.max_per_provider:
        return "provider cap reached"
    return None


def rank(ctx: JobContext, candidates: list[Candidate], policy: prof.CostPolicy, caps: Caps,
         now: datetime) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Pure ranking. Returns (ranked rows best-first, {device_id: exclusion reason})."""
    ranked: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}
    req = prof.requirements_for(ctx.terms, ctx.constraints)
    urgency = prof.urgency_multiplier(now, ctx.deadline_at)
    tier = str(ctx.constraints.get("tier", "STANDARD"))
    for c in candidates:
        reason = hard_exclusion(ctx, c, caps, now)
        if reason:
            excluded[str(c.device.id)] = reason
            continue
        est = prof.estimate_job(ctx.terms, c.profile, policy, gross_override=ctx.gross_override,
                                job_max_runtime=ctx.max_runtime, retry_count=ctx.retry_count)
        if not ctx.system and est.expected_net_profit_usd <= 0:
            excluded[str(c.device.id)] = f"unprofitable (net {est.expected_net_profit_usd:.8f})"
            continue
        opp_score = prof.score_opportunity(est, ctx.terms, c.profile) if not ctx.system else 1.0
        fit = prof.device_fit(c.profile, req)
        score = prof.final_score(opp_score, fit, c.profile.reputation, urgency, tier)
        ranked.append({"device": c.device, "estimate": est, "opportunity_score": opp_score, "fit": fit,
                       "score": score, "urgency": urgency, "tier": tier})
    ranked.sort(key=lambda r: (-r["score"], -(float(r["device"].farm_score or 0)), str(r["device"].id)))
    return ranked, excluded


# ── DB-backed tick ───────────────────────────────────────────────────────────

async def _job_context(db: AsyncSession, job: Job) -> tuple[JobContext, Optional[Any]]:
    opp = await opps.get_opportunity(db, job.opportunity_id) if job.opportunity_id else None
    if opp is None and job.workload_type not in SYSTEM_WORKLOADS:
        opp = await opps.internal_opportunity(db, job.workload_type)
    terms = opps.terms_from(opp, job.workload_type, job.constraints)
    c = dict(job.constraints or {})
    return JobContext(
        job_id=job.id, workload_type=job.workload_type, priority=int(job.priority or 0),
        created_at=job.created_at, terms=terms, deadline_at=job.deadline_at, constraints=c,
        excluded={str(x) for x in (job.verification_spec or {}).get("excluded_devices", [])},
        gross_override=float(c["customer_price_usd"]) if c.get("customer_price_usd") is not None else None,
        max_runtime=job.max_runtime_seconds, retry_count=int(job.retry_count or 0),
        system=job.workload_type in SYSTEM_WORKLOADS,
    ), opp


async def _candidates(db: AsyncSession, now: datetime) -> list[Candidate]:
    fresh = now - timedelta(seconds=settings.DEVICE_ONLINE_SECONDS)
    devices = (await db.execute(
        select(Device).where(Device.status.in_(list(SCHEDULABLE_STATUSES)), Device.last_seen_at >= fresh)
        .order_by(Device.id).limit(5000)
    )).scalars().all()
    ids = [d.id for d in devices]
    hbs = await econ.latest_heartbeats(db, ids)
    des = await econ.device_economics_rows(db, ids)
    busy_rows = (await db.execute(
        select(JobLease.device_id, func.count()).where(JobLease.status == leases.ACTIVE, JobLease.device_id.in_(ids))
        .group_by(JobLease.device_id))).all() if ids else []
    busy = {r[0]: int(r[1]) for r in busy_rows}
    out = []
    for d in devices:
        t = await bench_svc.thresholds_for_device(db, d)
        out.append(Candidate(
            device=d, profile=econ.device_profile(d, hbs.get(d.id), des.get(d.id)), busy=busy.get(d.id, 0),
            block_reason=(lambda wt, _d=d, _t=t: dispatch_block_reason(_d, wt, _t, now)),
        ))
    return out


async def _caps(db: AsyncSession) -> Caps:
    prov = (await db.execute(
        select(Device.provider_id, func.count()).join(JobLease, JobLease.device_id == Device.id)
        .where(JobLease.status == leases.ACTIVE).group_by(Device.provider_id))).all()
    wl = (await db.execute(select(Job.workload_type, func.count()).where(Job.status.in_(INFLIGHT))
                           .group_by(Job.workload_type))).all()
    return Caps(provider_inflight={str(r[0]): int(r[1]) for r in prov},
                workload_inflight={r[0]: int(r[1]) for r in wl},
                max_per_device=settings.MAX_CONCURRENT_JOBS_PER_DEVICE,
                max_per_provider=settings.MAX_INFLIGHT_PER_PROVIDER,
                workload_caps=dict(settings.WORKLOAD_INFLIGHT_CAPS))


async def _opportunity_inflight(db: AsyncSession, opportunity_id) -> int:
    return int((await db.execute(select(func.count()).select_from(Job).where(
        Job.opportunity_id == opportunity_id, Job.status.in_(INFLIGHT)))).scalar() or 0)


async def _assign(db: AsyncSession, job: Job, row: dict[str, Any], now: datetime) -> bool:
    """Transactional reserve + assign. Returns False on conflict (caller tries next device)."""
    # populate_existing: re-read the locked rows; never trust identity-map state.
    device = (await db.execute(select(Device).where(Device.id == row["device"].id).with_for_update()
                               .execution_options(populate_existing=True))).scalar_one_or_none()
    locked = (await db.execute(select(Job).where(Job.id == job.id).with_for_update()
                               .execution_options(populate_existing=True))).scalar_one_or_none()
    if locked is None or locked.status != JobStatus.QUEUED:
        return False
    if device is None or device.status not in SCHEDULABLE_STATUSES:
        return False
    active = int((await db.execute(select(func.count()).select_from(JobLease).where(
        JobLease.device_id == device.id, JobLease.status == leases.ACTIVE))).scalar() or 0)
    if active >= settings.MAX_CONCURRENT_JOBS_PER_DEVICE:
        return False  # reservation conflict: someone else took the capacity
    est: prof.RevenueEstimate = row["estimate"]
    leases.add_event(db, job.id, JobStatus.QUEUED.value, JobStatus.MATCHED.value,
                     reason=(f"scheduler: device {device.human_id} score {row['score']:.6g} "
                             f"(opp {row['opportunity_score']:.6g} x fit {row['fit']:.3f} x rep "
                             f"{float(device.reputation_score or 0):.1f}) net ${est.expected_net_profit_usd:.8f}")[:2000])
    db.add(JobAssignment(job_id=job.id, device_id=device.id, assigned_at=now))
    lease = await leases.issue_lease(db, job, device.id, now)
    leases.add_event(db, job.id, JobStatus.MATCHED.value, JobStatus.DISPATCHED.value,
                     reason=f"lease {lease.id} attempt {lease.attempt}")
    job.status = JobStatus.DISPATCHED
    job.updated_at = now
    job.last_error = None
    await econ.record_estimate(db, job, device.id, est, opportunity_score=row["opportunity_score"],
                               fit=row["fit"], final=row["score"],
                               inputs={"urgency": row["urgency"], "tier": row["tier"],
                                       "reputation": float(device.reputation_score or 0),
                                       "farm_score": float(device.farm_score) if device.farm_score is not None else None,
                                       "temperature_state": device.temperature_state})
    return True


async def scheduler_tick(db: AsyncSession, now: datetime | None = None, *, limit: int | None = None,
                         job_ids: list | None = None, dry_run: bool = False) -> dict[str, Any]:
    now = now or utcnow()
    limit = limit or settings.SCHEDULER_BATCH_LIMIT
    await opps.ensure_registry(db)
    q = select(Job).where(Job.status == JobStatus.QUEUED, Job.dead_lettered.is_(False))
    if job_ids:
        q = q.where(Job.id.in_([uuid.UUID(str(j)) for j in job_ids]))
    jobs = (await db.execute(q.order_by(Job.priority.desc(), Job.created_at).limit(limit * 5))).scalars().all()
    jobs = sorted(jobs, key=lambda j: (-effective_priority(int(j.priority or 0), j.created_at, now), j.created_at))[:limit]

    enabled_adapters = await opps.enabled_adapter_ids(db)
    policy = await opps.cost_policy(db)
    candidates = await _candidates(db, now) if jobs else []
    caps = await _caps(db) if jobs else Caps()
    report: dict[str, Any] = {"considered": len(jobs), "dispatched": [], "waiting": {}, "expired": [],
                              "devices_online": len(candidates), "dry_run": dry_run, "at": now.isoformat()}

    for job in jobs:
        jid = str(job.id)
        if job.deadline_at is not None and now >= job.deadline_at:
            if not dry_run:
                leases.add_event(db, job.id, JobStatus.QUEUED.value, JobStatus.EXPIRED.value,
                                 reason="deadline passed before dispatch")
                job.status = JobStatus.EXPIRED
                job.last_error = "deadline passed before dispatch"
                await db.commit()
            report["expired"].append(jid)
            continue
        role = (job.verification_spec or {}).get("role", "PRIMARY")
        if role == "PRIMARY" and job.adapter_id not in enabled_adapters:
            report["waiting"][jid] = f"adapter {job.adapter_id} disabled (kill switch)"
            continue
        ctx, opp = await _job_context(db, job)
        block = opps.opportunity_block_reason(opp, now)
        if block:
            report["waiting"][jid] = block
            continue
        if opp is not None and opp.available_capacity is not None and \
                await _opportunity_inflight(db, opp.id) >= int(opp.available_capacity):
            report["waiting"][jid] = "opportunity capacity full"
            continue
        wcap = caps.workload_caps.get(job.workload_type)
        if wcap is not None and caps.workload_inflight.get(job.workload_type, 0) >= wcap:
            report["waiting"][jid] = f"workload cap {wcap} reached"
            continue

        ranked, excluded = rank(ctx, candidates, policy, caps, now)
        if not ranked:
            reasons: dict[str, int] = {}
            for r in excluded.values():
                key = r.split(" (")[0] if r.startswith("unprofitable") else r.split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
            msg = "no eligible device" + (f": {reasons}" if reasons else " online")
            report["waiting"][jid] = msg
            if not dry_run and job.last_error != msg[:2000]:
                job.last_error = msg[:2000]
                await db.commit()
            continue
        if dry_run:
            top = ranked[0]
            report["dispatched"].append({"job_id": jid, "device_id": str(top["device"].id), "score": top["score"],
                                         "expected_net_usd": top["estimate"].expected_net_profit_usd})
            continue

        assigned = None
        for row in ranked:
            ok = False
            try:
                async with db.begin_nested():  # savepoint: a conflict never expires other state
                    ok = await _assign(db, job, row, now)
                    if not ok:
                        raise _Conflict()
            except _Conflict:
                ok = False
            except Exception:
                logger.exception("scheduler_assign_failed", job_id=jid)
                await db.commit()
                break
            await db.commit()  # persist the assignment / release row locks
            if ok:
                assigned = row
                break
            st = (await db.execute(select(Job.status).where(Job.id == job.id))).scalar()
            if st != JobStatus.QUEUED:
                break  # someone else dispatched it
        if assigned is None:
            report["waiting"][jid] = "reservation conflict"
            continue
        dev = assigned["device"]
        for c in candidates:
            if c.device.id == dev.id:
                c.busy += 1
        caps.provider_inflight[str(dev.provider_id)] = caps.provider_inflight.get(str(dev.provider_id), 0) + 1
        caps.workload_inflight[job.workload_type] = caps.workload_inflight.get(job.workload_type, 0) + 1
        report["dispatched"].append({"job_id": jid, "device_id": str(dev.id), "device_human_id": dev.human_id,
                                     "score": assigned["score"],
                                     "expected_net_usd": assigned["estimate"].expected_net_profit_usd})
        logger.info("scheduler_dispatched", job_id=jid, device_id=str(dev.id), score=assigned["score"])
    return report


async def preview(db: AsyncSession, job: Job, now: datetime | None = None) -> dict[str, Any]:
    """Auditable ranking for one job without assigning anything."""
    now = now or utcnow()
    ctx, opp = await _job_context(db, job)
    policy = await opps.cost_policy(db)
    candidates = await _candidates(db, now)
    ranked, excluded = rank(ctx, candidates, policy, await _caps(db), now)
    return {
        "job_id": str(job.id), "status": job.status.value, "workload_type": job.workload_type,
        "opportunity_id": str(opp.id) if opp else None, "adapter_enabled": await opps.adapter_enabled(db, job.adapter_id),
        "opportunity_block": opps.opportunity_block_reason(opp, now),
        "ranked": [{"device_id": str(r["device"].id), "human_id": r["device"].human_id, "score": r["score"],
                    "opportunity_score": r["opportunity_score"], "device_fit": r["fit"],
                    "expected_net_usd": r["estimate"].expected_net_profit_usd,
                    "expected_costs": r["estimate"].costs} for r in ranked[:20]],
        "excluded": excluded,
    }


async def release_resources(db: AsyncSession, device_id, job_id) -> None:  # spec §57.4 contract
    """Capacity is held by the ACTIVE lease; closing it releases the device."""
    lease = await leases.current_lease(db, job_id, device_id)
    if lease is not None:
        leases.close_lease(lease, leases.RELEASED, "RELEASED")
        await leases._release_device(db, device_id)
