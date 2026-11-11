"""Opportunity registry + adapter kill switch (spec §13, §14, §57.3, §126).

* ``ensure_registry`` upserts every registered adapter into ``revenue_adapters``
  and runs discovery once so internal opportunities always exist.
* Disabled adapters: no new jobs, no dispatch, no scheduling (in-flight work
  finishes; history is retained) — spec §126 runbook.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import guard, registry
from app.adapters.base import OpportunitySpec
from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.core import SystemSettings
from app.db.models.enums import AuditEventType, OpportunityStatus
from app.db.models.jobs_ledger import Job, Opportunity, RevenueAdapterRecord
from app.logging import get_logger
from app.services import profitability as prof

logger = get_logger(__name__)

POLICY_KEY = "profitability_policy"
INTERNAL_ADAPTER_ID = "internal_compute"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── policy ───────────────────────────────────────────────────────────────────

async def get_policy_override(db: AsyncSession) -> dict[str, Any]:
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == POLICY_KEY))).scalar_one_or_none()
    return dict(row.value or {}) if row else {}


async def cost_policy(db: AsyncSession) -> prof.CostPolicy:
    return prof.CostPolicy.from_settings(settings, await get_policy_override(db))


async def set_policy_override(db: AsyncSession, value: dict[str, Any], user_id: UUID | None) -> prof.CostPolicy:
    allowed = set(prof.CostPolicy().as_dict().keys())
    bad = [k for k in value if k not in allowed]
    if bad:
        raise ValueError(f"unknown policy keys: {', '.join(bad)}")
    for k, v in value.items():
        if isinstance(v, dict):
            if any(float(x) < 0 for x in v.values()):
                raise ValueError(f"{k} values must be >= 0")
        elif v is not None and float(v) < 0:
            raise ValueError(f"{k} must be >= 0")
    policy = prof.CostPolicy.from_settings(settings, value)  # validates types
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == POLICY_KEY))).scalar_one_or_none()
    if row is None:
        db.add(SystemSettings(key=POLICY_KEY, value=value, description="Profitability cost policy override",
                              updated_by=user_id))
    else:
        row.value = value
        row.updated_by = user_id
    return policy


# ── adapters ─────────────────────────────────────────────────────────────────

async def ensure_registry(db: AsyncSession, *, discover: bool = True) -> None:
    existing = {r.id: r for r in (await db.execute(select(RevenueAdapterRecord))).scalars().all()}
    for a in registry.ADAPTERS.values():
        if a.id not in existing:
            # concurrent first requests on a fresh DB race here: insert in a savepoint
            # and treat a unique violation as "another request created it"
            # (found by the Sprint 10 fleet load test)
            try:
                async with db.begin_nested():
                    db.add(RevenueAdapterRecord(id=a.id, name=a.name, category=a.category, kind=a.kind,
                                                simulated=a.simulated, enabled=True,
                                                certification_stage=a.certification_stage, health_detail={},
                                                last_reconcile={}))
                    await db.flush()
            except IntegrityError:
                pass
    if discover:  # first run: seed every adapter's catalogue (all local / simulated; no network)
        seeded = {r[0] for r in (await db.execute(select(Opportunity.adapter_id).distinct())).all()}
        for aid in registry.ADAPTERS:
            if aid not in seeded:
                await refresh_adapter(db, aid)


async def adapter_record(db: AsyncSession, adapter_id: str) -> Optional[RevenueAdapterRecord]:
    return (await db.execute(select(RevenueAdapterRecord).where(RevenueAdapterRecord.id == adapter_id))).scalar_one_or_none()


def _schedulable(rec: Optional[RevenueAdapterRecord], external_paused: bool) -> bool:
    """New jobs may be created / scheduled / dispatched for this adapter (spec §126, §139)."""
    if rec is None or not rec.enabled:
        return False
    if guard.switch_on(rec, "DISABLE_SCHEDULING"):
        return False
    if rec.kind == "EXTERNAL" and external_paused:
        return False
    return True


async def adapter_enabled(db: AsyncSession, adapter_id: str) -> bool:
    if adapter_id in registry.SYSTEM_ADAPTER_IDS:
        return True
    rec = await adapter_record(db, adapter_id)
    if rec is None and adapter_id in registry.ADAPTERS:
        await ensure_registry(db, discover=False)
        rec = await adapter_record(db, adapter_id)
    return _schedulable(rec, await guard.external_paused(db))


async def enabled_adapter_ids(db: AsyncSession) -> set[str]:
    paused = await guard.external_paused(db)
    rows = (await db.execute(select(RevenueAdapterRecord).where(RevenueAdapterRecord.enabled.is_(True)))).scalars().all()
    return {r.id for r in rows if _schedulable(r, paused)} | registry.SYSTEM_ADAPTER_IDS


async def earnings_release_block(db: AsyncSession, adapter_id: Optional[str],
                                 _cache: Optional[dict[str, Optional[str]]] = None) -> Optional[str]:
    """Spec §126: while an EXTERNAL adapter is disabled, globally paused or its last
    reconciliation is not clean, its earnings must not be converted to AVAILABLE."""
    if not adapter_id:
        return None
    if _cache is not None and adapter_id in _cache:
        return _cache[adapter_id]
    a = registry.get(adapter_id)
    reason: Optional[str] = None
    if a is not None and a.kind == "EXTERNAL":
        rec = await adapter_record(db, adapter_id)
        if rec is None or not rec.enabled:
            reason = f"adapter {adapter_id} disabled (spec §126)"
        elif await guard.external_paused(db):
            reason = "PAUSE_EXTERNAL_ADAPTERS kill switch"
        elif (rec.last_reconcile or {}).get("status") in ("MISMATCH", "ERROR"):
            reason = (f"adapter {adapter_id} last reconciliation "
                      f"{(rec.last_reconcile or {}).get('status')} (spec §126)")
    if _cache is not None:
        _cache[adapter_id] = reason
    return reason


async def set_adapter_enabled(db: AsyncSession, adapter_id: str, enabled: bool, reason: str | None,
                              user_id: UUID | None, actor_type: str = "ADMIN") -> RevenueAdapterRecord:
    await ensure_registry(db, discover=False)
    rec = await adapter_record(db, adapter_id)
    if rec is None:
        raise KeyError(adapter_id)
    rec.enabled = enabled
    if enabled:  # an admin re-enable closes the circuit breaker
        guard.reset_circuit(rec)
    rec.disabled_reason = None if enabled else (reason or "disabled by admin")
    rec.disabled_by = None if enabled else user_id
    rec.disabled_at = None if enabled else utcnow()
    db.add(AuditLog(event_type=AuditEventType.ADAPTER_ENABLED if enabled else AuditEventType.ADAPTER_DISABLED,
                    actor_type=actor_type, actor_id=user_id, target_type="ADAPTER", target_id=None,
                    event_metadata={"adapter_id": adapter_id, "reason": reason}))
    logger.warning("adapter_kill_switch", adapter_id=adapter_id, enabled=enabled, reason=reason)
    return rec


async def health_check(db: AsyncSession, adapter_id: str) -> dict[str, Any]:
    adapter = registry.get(adapter_id)
    rec = await adapter_record(db, adapter_id)
    if adapter is None or rec is None:
        raise KeyError(adapter_id)
    try:
        h = await guard.call(db, rec, "health_check", adapter.health_check)
        if not isinstance(h, dict):
            await guard.record_failure(db, rec, "health_check", f"returned {type(h).__name__}")
            h = {"status": "ERROR", "error": "health_check returned a non-object"}
    except guard.AdapterCallError as exc:  # adapter failure must not crash admin
        h = {"status": "CIRCUIT_OPEN" if exc.short_circuited else "ERROR", "error": exc.detail[:500]}
    rec.health_status = str(h.get("status", "UNKNOWN"))
    rec.health_detail = h
    rec.last_health_at = utcnow()
    return h


async def reconcile(db: AsyncSession, adapter_id: str, user_id: UUID | None = None) -> dict[str, Any]:
    """Compare expected token earnings with the adapter's (simulated) balance.
    Mismatch -> automatic kill switch (spec §126: disable new scheduling)."""
    from sqlalchemy import func

    from app.db.models.jobs_ledger import JobEconomics

    adapter = registry.get(adapter_id)
    rec = await adapter_record(db, adapter_id)
    if adapter is None or rec is None:
        raise KeyError(adapter_id)
    rows = (await db.execute(
        select(Job.opportunity_id, func.count(JobEconomics.job_id))
        .join(JobEconomics, JobEconomics.job_id == Job.id)
        .where(JobEconomics.adapter_id == adapter_id, JobEconomics.status == "ACTUAL")
        .group_by(Job.opportunity_id)
    )).all()
    expected = 0.0
    for opp_id, n in rows:
        opp = (await db.execute(select(Opportunity).where(Opportunity.id == opp_id))).scalar_one_or_none() if opp_id else None
        per_job = float((opp.constraints or {}).get("reward_asset_per_job") or 0) if opp else 0.0
        expected += per_job * int(n)
    try:
        res = await guard.call(db, rec, "reconcile", lambda: adapter.reconcile(expected))
    except guard.AdapterCallError as exc:
        # an unreachable counterparty is not a clean reconciliation: record it so §126
        # keeps that adapter's pending earnings from becoming AVAILABLE
        out = {"status": "ERROR", "expected_asset": expected, "external_asset": None, "difference_asset": None,
               "detail": exc.detail[:500], "simulated": rec.simulated, "at": utcnow().isoformat()}
        rec.last_reconcile = out
        return out
    out = {"status": res.status, "expected_asset": res.expected_asset, "external_asset": res.external_asset,
           "difference_asset": res.difference_asset, "detail": res.detail, "simulated": res.simulated,
           "at": utcnow().isoformat()}
    rec.last_reconcile = out
    if res.status == "MISMATCH" and rec.enabled:
        await set_adapter_enabled(db, adapter_id, False, f"auto: reconciliation mismatch {res.difference_asset:.6f}",
                                  user_id, actor_type="SYSTEM")
        out["auto_disabled"] = True
    return out


async def claim(db: AsyncSession, adapter_id: str, user_id: UUID | None = None,
                now: Optional[datetime] = None) -> dict[str, Any]:
    """Claim confirmed work from an adapter (simulated adapters return a SIMULATED
    receipt; no network, no wallet). Refused while the adapter is disabled, its
    DISABLE_CLAIMS switch is on, PAUSE_EXTERNAL_ADAPTERS is on, or its last
    reconciliation is not clean. Uses a finalized_at watermark so work is never
    claimed twice."""
    from app.db.models.jobs_ledger import JobEconomics

    adapter = registry.get(adapter_id)
    rec = await adapter_record(db, adapter_id)
    if adapter is None or rec is None:
        raise KeyError(adapter_id)
    now = now or utcnow()
    if not rec.enabled:
        raise ValueError("adapter disabled")
    if guard.switch_on(rec, "DISABLE_CLAIMS"):
        raise ValueError("DISABLE_CLAIMS is on for this adapter")
    if rec.kind == "EXTERNAL" and await guard.external_paused(db):
        raise ValueError("PAUSE_EXTERNAL_ADAPTERS kill switch is on")
    if (rec.last_reconcile or {}).get("status") in ("MISMATCH", "ERROR"):
        raise ValueError("last reconciliation is not clean; reconcile before claiming (spec §126)")
    prev = dict(rec.last_claim or {})
    q = (select(JobEconomics, Job.opportunity_id).join(Job, Job.id == JobEconomics.job_id)
         .where(JobEconomics.adapter_id == adapter_id, JobEconomics.status == "ACTUAL",
                JobEconomics.finalized_at.is_not(None), JobEconomics.finalized_at <= now)
         .order_by(JobEconomics.finalized_at))
    if prev.get("through"):
        q = q.where(JobEconomics.finalized_at > datetime.fromisoformat(prev["through"]))
    rows = (await db.execute(q)).all()
    per_job: dict[Any, float] = {}
    work = []
    for econ, opp_id in rows:
        if opp_id not in per_job:
            opp = await get_opportunity(db, opp_id) if opp_id else None
            per_job[opp_id] = float((opp.constraints or {}).get("reward_asset_per_job") or 0) if opp else 0.0
        work.append({"job_id": str(econ.job_id), "amount_asset": per_job[opp_id]})
    if not work:
        return {"status": "NOTHING_TO_CLAIM", "jobs": 0, "simulated": rec.simulated}
    try:
        res = await guard.call(db, rec, "claim", lambda: adapter.claim(work), now=now)
    except guard.AdapterCallError as exc:
        return {"status": "ERROR", "error": exc.detail[:500], "jobs": len(work)}
    through = max(e.finalized_at for e, _ in rows)
    out = {"status": res.status, "jobs": len(work), "amount_asset": res.amount_asset, "asset": res.asset_symbol,
           "fee_usd": res.fee_usd, "receipt_ref": res.receipt_id, "simulated": res.simulated,
           "through": through.isoformat(), "at": now.isoformat(),
           "claims_total": int(prev.get("claims_total") or 0) + 1}
    rec.last_claim = out
    db.add(AuditLog(event_type=AuditEventType.ADAPTER_CLAIMED, actor_type="ADMIN" if user_id else "SYSTEM",
                    actor_id=user_id, target_type="ADAPTER", event_metadata={"adapter_id": adapter_id, **out}))
    return out


# ── opportunities ────────────────────────────────────────────────────────────

def _spec_constraints(spec: OpportunitySpec) -> dict[str, Any]:
    c = dict(spec.constraints or {})
    if spec.reward_asset_per_job is not None:
        c["reward_asset_per_job"] = spec.reward_asset_per_job
    return c


async def refresh_adapter(db: AsyncSession, adapter_id: str) -> list[Opportunity]:
    adapter = registry.get(adapter_id)
    if adapter is None:
        raise KeyError(adapter_id)
    await ensure_registry(db, discover=False)
    rec = await adapter_record(db, adapter_id)
    if rec is not None and not rec.enabled:
        return []  # killed adapters are not refreshed
    if rec is not None and guard.switch_on(rec, "DISABLE_DISCOVERY"):
        return []
    if rec is not None and rec.kind == "EXTERNAL" and await guard.external_paused(db):
        return []
    now = utcnow()
    if rec is None:
        specs = await adapter.discover()
    else:
        try:
            specs = await guard.call(db, rec, "discover", adapter.discover)
        except guard.AdapterCallError as exc:
            rec.health_detail = {**(rec.health_detail or {}), "last_discovery_error": exc.detail[:500]}
            return []  # existing opportunities are retained unchanged
    checked = guard.check_specs(specs)
    if rec is not None:
        rec.health_detail = {**(rec.health_detail or {}), "last_discovery": {
            "at": now.isoformat(), "accepted": len(checked.accepted), "rejected": checked.rejected[:20],
            "rejected_count": len(checked.rejected), "truncated": checked.truncated}}
        if checked.rejected and not checked.accepted:
            await guard.record_failure(db, rec, "discover", f"all {len(checked.rejected)} opportunities invalid")
    out: list[Opportunity] = []
    for spec in checked.accepted:
        opp = (await db.execute(select(Opportunity).where(
            Opportunity.adapter_id == adapter_id, Opportunity.external_id == spec.external_id))).scalar_one_or_none()
        if opp is None:
            try:  # race-safe first insert (uq_adapter_external), see ensure_registry
                async with db.begin_nested():
                    opp = Opportunity(adapter_id=adapter_id, external_id=spec.external_id, discovered_at=now,
                                      status=OpportunityStatus.ACTIVE, name=spec.name, category=spec.category)
                    db.add(opp)
                    await db.flush()
            except IntegrityError:
                opp = (await db.execute(select(Opportunity).where(
                    Opportunity.adapter_id == adapter_id, Opportunity.external_id == spec.external_id)
                    .execution_options(populate_existing=True))).scalar_one()
        manually_disabled = opp.status == OpportunityStatus.DISABLED
        opp.name, opp.category, opp.workload_type = spec.name, spec.category, spec.workload_type
        opp.asset_symbol = spec.asset_symbol
        opp.revenue_usd_per_job = spec.revenue_usd_per_job
        opp.estimated_revenue_usd_hour = spec.estimated_revenue_usd_hour
        opp.expected_runtime_seconds = spec.expected_runtime_seconds
        opp.thermal_intensity = spec.thermal_intensity
        opp.payment_confidence = spec.payment_confidence
        opp.repeatability_score = spec.repeatability_score
        opp.expected_payment_days = spec.expected_payment_days
        opp.risk_score = spec.risk_score
        opp.normalized_resource_consumption = spec.normalized_resource_consumption
        opp.estimated_api_cost_usd = spec.estimated_api_cost_usd
        opp.expected_network_bytes = spec.expected_network_bytes
        opp.min_farm_score = spec.min_farm_score
        opp.resource_requirements = spec.resource_requirements
        opp.constraints = _spec_constraints(spec)
        opp.payload_template = spec.payload_template
        opp.available_capacity = spec.available_capacity
        opp.expires_at = spec.expires_at
        opp.region_rules = opp.region_rules or {}
        await db.flush()
        if not manually_disabled:
            try:
                v = await adapter.validate(opp)
            except Exception as exc:  # untrusted adapter code
                from app.adapters.base import ValidationResult
                v = ValidationResult(False, [f"validate() failed: {type(exc).__name__}: {str(exc)[:200]}"])
            opp.status = OpportunityStatus.ACTIVE if v.ok else OpportunityStatus.UNSCORABLE
            opp.disabled_reason = None if v.ok else "; ".join(v.reasons)
        out.append(opp)
    if rec is not None:
        rec.last_discovery_at = now
    await db.flush()
    return out


async def discover_all(db: AsyncSession) -> dict[str, int]:
    await ensure_registry(db, discover=False)
    return {aid: len(await refresh_adapter(db, aid)) for aid in registry.ADAPTERS}


def terms_from(opp: Optional[Opportunity], workload_type: str, job_constraints: dict | None = None) -> prof.OpportunityTerms:
    if opp is None:
        return prof.OpportunityTerms(adapter_id=INTERNAL_ADAPTER_ID, workload_type=workload_type)
    c = opp.constraints or {}
    f = lambda v, d: float(v) if v is not None else d  # noqa: E731
    return prof.OpportunityTerms(
        adapter_id=opp.adapter_id,
        workload_type=opp.workload_type or workload_type,
        asset_symbol=opp.asset_symbol or "USD",
        revenue_usd_per_job=f(opp.revenue_usd_per_job, None),
        reward_asset_per_job=f(c.get("reward_asset_per_job"), None),
        estimated_revenue_usd_hour=f(opp.estimated_revenue_usd_hour, None),
        expected_runtime_seconds=opp.expected_runtime_seconds,
        thermal_intensity=f(opp.thermal_intensity, 0.3),
        payment_confidence=f(opp.payment_confidence, 0.5),
        repeatability_score=f(opp.repeatability_score, 0.8),
        expected_payment_days=f(opp.expected_payment_days, 0.0),
        risk_score=f(opp.risk_score, 0.5),
        normalized_resource_consumption=f(opp.normalized_resource_consumption, 0.5),
        estimated_api_cost_usd=f(opp.estimated_api_cost_usd, 0.0),
        expected_network_bytes=int(opp.expected_network_bytes or 0),
        chain_fee_usd_per_job=f(c.get("chain_fee_usd_per_job"), 0.0),
        payout_fee_pct=f(c.get("payout_fee_pct"), None),
        min_farm_score=f(opp.min_farm_score, None),
        requirements=dict(opp.resource_requirements or {}),
    )


async def get_opportunity(db: AsyncSession, opportunity_id: UUID) -> Optional[Opportunity]:
    return (await db.execute(select(Opportunity).where(Opportunity.id == opportunity_id))).scalar_one_or_none()


async def internal_opportunity(db: AsyncSession, workload_type: str) -> Optional[Opportunity]:
    await ensure_registry(db)
    return (await db.execute(select(Opportunity).where(
        Opportunity.adapter_id == INTERNAL_ADAPTER_ID, Opportunity.external_id == workload_type))).scalar_one_or_none()


def opportunity_block_reason(opp: Optional[Opportunity], now: datetime) -> Optional[str]:
    if opp is None:
        return None
    if opp.status != OpportunityStatus.ACTIVE:
        return f"opportunity {opp.status.value}" + (f": {opp.disabled_reason}" if opp.disabled_reason else "")
    if opp.expires_at is not None and now >= opp.expires_at:
        return "opportunity expired"
    return None


async def set_opportunity_enabled(db: AsyncSession, opp: Opportunity, enabled: bool, reason: str | None) -> Opportunity:
    opp.status = OpportunityStatus.ACTIVE if enabled else OpportunityStatus.DISABLED
    opp.disabled_reason = None if enabled else (reason or "disabled by admin")
    return opp


def opportunity_dict(opp: Opportunity, reference: Optional[prof.RevenueEstimate] = None,
                     score: Optional[float] = None, adapter_enabled: Optional[bool] = None) -> dict[str, Any]:
    f = lambda v: float(v) if v is not None else None  # noqa: E731
    d = {
        "opportunity_id": str(opp.id), "adapter_id": opp.adapter_id, "external_id": opp.external_id,
        "name": opp.name, "category": opp.category, "workload_type": opp.workload_type,
        "asset_symbol": opp.asset_symbol, "status": opp.status.value, "disabled_reason": opp.disabled_reason,
        "revenue_usd_per_job": f(opp.revenue_usd_per_job),
        "reward_asset_per_job": f((opp.constraints or {}).get("reward_asset_per_job")),
        "estimated_revenue_usd_hour": f(opp.estimated_revenue_usd_hour),
        "payment_confidence": f(opp.payment_confidence), "risk_score": f(opp.risk_score),
        "repeatability_score": f(opp.repeatability_score), "expected_payment_days": f(opp.expected_payment_days),
        "thermal_intensity": f(opp.thermal_intensity), "min_farm_score": f(opp.min_farm_score),
        "available_capacity": opp.available_capacity, "constraints": opp.constraints or {},
        "simulated": bool((opp.constraints or {}).get("simulated")),
        "adapter_enabled": adapter_enabled,
        "expires_at": opp.expires_at.isoformat() if opp.expires_at else None,
    }
    if reference is not None:
        d["reference_estimate"] = {
            "gross_usd": reference.gross_usd, "adjusted_gross_usd": reference.adjusted_gross_usd,
            "total_cost_usd": reference.total_cost_usd, "expected_net_usd": reference.expected_net_profit_usd,
            "runtime_seconds": reference.runtime_seconds, "costs": reference.costs,
            "profitable": reference.profitable,
        }
        d["opportunity_score"] = score
    return d
