"""External adapter hardening (Sprint 10; spec §126, §127, §128, §139).

Every call FarmOS makes into a revenue adapter goes through :func:`call`:

* **bounded** — ``asyncio.wait_for`` with ``ADAPTER_CALL_TIMEOUT_SECONDS``;
* **contained** — any exception becomes :class:`AdapterCallError`, never a crash
  of discovery, admin, scheduling or finance;
* **accounted** — call / error / consecutive-failure counters + last error are
  persisted on the adapter record;
* **circuit breaker** — after ``ADAPTER_CIRCUIT_FAILURE_THRESHOLD`` consecutive
  failures the circuit opens for ``ADAPTER_CIRCUIT_COOLDOWN_SECONDS``; calls are
  short-circuited while open. An EXTERNAL adapter whose circuit opens is also
  auto-disabled (spec §126 step 1: disable new scheduling) by the SYSTEM actor,
  and stays disabled until an admin re-enables it (which resets the breaker).

It also holds the granular per-adapter switches (spec §139: DISABLE_DISCOVERY,
DISABLE_SCHEDULING, DISABLE_CLAIMS), the global PAUSE_EXTERNAL_ADAPTERS check,
validation of discovered opportunity specs, and the certification pipeline
(spec §127 stages; simulated adapters can never pass SANDBOX).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import OpportunitySpec
from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.enums import AuditEventType
from app.db.models.jobs_ledger import RevenueAdapterRecord
from app.logging import get_logger

logger = get_logger(__name__)
T = TypeVar("T")

ADAPTER_SWITCHES = ("DISABLE_DISCOVERY", "DISABLE_SCHEDULING", "DISABLE_CLAIMS")
CERTIFICATION_STAGES = ("PROPOSED", "TECHNICAL_REVIEW", "POLICY_REVIEW", "ECONOMIC_REVIEW", "SANDBOX",
                        "CANARY", "LIMITED_PRODUCTION", "APPROVED")
# spec §127 checklist; every item must be answered before an adapter may leave ECONOMIC_REVIEW
CERTIFICATION_CHECKLIST = (
    "terms_permit_automation", "terms_permit_mobile_devices", "requires_kyc", "requires_custody",
    "token_rewards", "payment_conditions_documented", "rewards_verifiable", "workload_isolated",
    "dangerous_permissions", "geographic_restrictions", "adapter_disableable_without_deploy",
    "safe_failure_behavior",
)
# spec §128 economic certification inputs (must be present before leaving ECONOMIC_REVIEW)
ECONOMIC_FIELDS = ("gross_revenue_per_task_usd", "power_cost_per_task_usd", "battery_wear_per_task_usd",
                   "network_cost_per_task_usd", "payout_fee_per_task_usd", "payment_delay_days",
                   "reward_volatility", "minimum_profitable_farm_score")
SIMULATED_MAX_STAGE = "SANDBOX"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdapterCallError(Exception):
    def __init__(self, adapter_id: str, op: str, detail: str, *, short_circuited: bool = False):
        super().__init__(f"{adapter_id}.{op}: {detail}")
        self.adapter_id, self.op, self.detail, self.short_circuited = adapter_id, op, detail, short_circuited


# ── switches ────────────────────────────────────────────────────────────────

def switches(rec: Optional[RevenueAdapterRecord]) -> dict[str, bool]:
    raw = dict((rec.switches or {}) if rec is not None else {})
    return {k: bool(raw.get(k, False)) for k in ADAPTER_SWITCHES}


def switch_on(rec: Optional[RevenueAdapterRecord], name: str) -> bool:
    return switches(rec)[name]


async def external_paused(db: AsyncSession) -> bool:
    from app.services.finance import controls
    return await controls.is_paused(db, "PAUSE_EXTERNAL_ADAPTERS")


def circuit_open(rec: Optional[RevenueAdapterRecord], now: Optional[datetime] = None) -> bool:
    if rec is None or rec.circuit_open_until is None:
        return False
    return (now or utcnow()) < rec.circuit_open_until


def reset_circuit(rec: RevenueAdapterRecord) -> None:
    rec.consecutive_failures = 0
    rec.circuit_open_until = None


async def set_switch(db: AsyncSession, rec: RevenueAdapterRecord, name: str, enabled: bool,
                     reason: Optional[str], actor_id: Any, actor_type: str = "ADMIN") -> dict[str, bool]:
    if name not in ADAPTER_SWITCHES:
        raise ValueError(f"unknown adapter switch {name}; expected one of {', '.join(ADAPTER_SWITCHES)}")
    cur = dict(rec.switches or {})
    cur[name] = bool(enabled)
    rec.switches = cur  # reassign so the JSON change is persisted
    db.add(AuditLog(event_type=AuditEventType.ADAPTER_SWITCH_CHANGED, actor_type=actor_type, actor_id=actor_id,
                    target_type="ADAPTER", event_metadata={"adapter_id": rec.id, "switch": name,
                                                           "enabled": bool(enabled), "reason": reason}))
    logger.warning("adapter_switch", adapter_id=rec.id, switch=name, enabled=enabled, reason=reason)
    return switches(rec)


# ── guarded call + circuit breaker ──────────────────────────────────────────

async def call(db: AsyncSession, rec: RevenueAdapterRecord, op: str, fn: Callable[[], Awaitable[T]],
               *, timeout: Optional[float] = None, now: Optional[datetime] = None) -> T:
    """Run one adapter call with timeout, accounting and circuit breaking."""
    now = now or utcnow()
    if circuit_open(rec, now):
        raise AdapterCallError(rec.id, op, f"circuit open until {rec.circuit_open_until.isoformat()}",
                               short_circuited=True)
    rec.call_count = int(rec.call_count or 0) + 1
    limit = float(timeout if timeout is not None else settings.ADAPTER_CALL_TIMEOUT_SECONDS)
    try:
        result = await asyncio.wait_for(fn(), timeout=limit)
    except asyncio.TimeoutError:
        await _record_failure(db, rec, op, f"timeout after {limit:g}s", now)
        raise AdapterCallError(rec.id, op, f"timeout after {limit:g}s") from None
    except AdapterCallError:
        raise
    except Exception as exc:  # adapter code is untrusted: contain every failure
        detail = f"{type(exc).__name__}: {str(exc)[:400]}"
        await _record_failure(db, rec, op, detail, now)
        raise AdapterCallError(rec.id, op, detail) from exc
    rec.consecutive_failures = 0
    rec.circuit_open_until = None
    return result


async def record_failure(db: AsyncSession, rec: RevenueAdapterRecord, op: str, detail: str,
                         now: Optional[datetime] = None) -> None:
    """Count a failure detected after a call returned (e.g. garbage output)."""
    await _record_failure(db, rec, op, detail, now or utcnow())


async def _record_failure(db: AsyncSession, rec: RevenueAdapterRecord, op: str, detail: str, now: datetime) -> None:
    rec.error_count = int(rec.error_count or 0) + 1
    rec.consecutive_failures = int(rec.consecutive_failures or 0) + 1
    rec.last_error = f"{op}: {detail}"[:1000]
    rec.last_error_at = now
    logger.warning("adapter_call_failed", adapter_id=rec.id, op=op, detail=detail,
                   consecutive=rec.consecutive_failures)
    if rec.consecutive_failures < settings.ADAPTER_CIRCUIT_FAILURE_THRESHOLD:
        return
    rec.circuit_open_until = now + timedelta(seconds=settings.ADAPTER_CIRCUIT_COOLDOWN_SECONDS)
    auto_disable = rec.kind == "EXTERNAL" and rec.enabled
    db.add(AuditLog(event_type=AuditEventType.ADAPTER_CIRCUIT_OPENED, actor_type="SYSTEM", target_type="ADAPTER",
                    event_metadata={"adapter_id": rec.id, "op": op, "detail": detail[:500],
                                    "consecutive_failures": rec.consecutive_failures,
                                    "open_until": rec.circuit_open_until.isoformat(),
                                    "auto_disabled": auto_disable}))
    if auto_disable:
        from app.services import opportunity_service as opps
        await opps.set_adapter_enabled(db, rec.id, False,
                                       f"auto: circuit opened after {rec.consecutive_failures} consecutive "
                                       f"failures ({op}: {detail[:200]})", None, actor_type="SYSTEM")


# ── discovery output validation ─────────────────────────────────────────────

@dataclass
class SpecCheck:
    accepted: list[OpportunitySpec]
    rejected: list[dict[str, Any]]
    truncated: int = 0


def _num(v: Any, name: str, lo: float, hi: float, errs: list[str], *, optional: bool = False) -> None:
    if v is None:
        if not optional:
            errs.append(f"{name} missing")
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        errs.append(f"{name} must be a finite number")
    elif not (lo <= float(v) <= hi):
        errs.append(f"{name} out of range [{lo:g}, {hi:g}]")


def validate_spec(spec: Any) -> list[str]:
    """Structural / range validation of one discovered opportunity (adapters are untrusted)."""
    from app.workloads import cpu_data

    if not isinstance(spec, OpportunitySpec):
        return [f"not an OpportunitySpec ({type(spec).__name__})"]
    errs: list[str] = []
    for name, maxlen in (("external_id", 255), ("name", 255), ("category", 100), ("workload_type", 50),
                         ("asset_symbol", 20)):
        v = getattr(spec, name)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{name} must be a non-empty string")
        elif len(v) > maxlen:
            errs.append(f"{name} longer than {maxlen}")
    _num(spec.revenue_usd_per_job, "revenue_usd_per_job", 0, 1000, errs, optional=True)
    _num(spec.reward_asset_per_job, "reward_asset_per_job", 0, 1e9, errs, optional=True)
    _num(spec.estimated_revenue_usd_hour, "estimated_revenue_usd_hour", 0, 10000, errs, optional=True)
    _num(spec.expected_runtime_seconds, "expected_runtime_seconds", 1, 86400, errs, optional=True)
    for name in ("thermal_intensity", "payment_confidence", "repeatability_score", "risk_score"):
        _num(getattr(spec, name), name, 0, 1, errs)
    _num(spec.normalized_resource_consumption, "normalized_resource_consumption", 0, 10, errs)
    _num(spec.expected_payment_days, "expected_payment_days", 0, 3650, errs)
    _num(spec.estimated_api_cost_usd, "estimated_api_cost_usd", 0, 1000, errs)
    _num(spec.expected_network_bytes, "expected_network_bytes", 0, 10 * 1024 ** 3, errs)
    _num(spec.min_farm_score, "min_farm_score", 0, 100, errs, optional=True)
    _num(spec.available_capacity, "available_capacity", 0, 1_000_000, errs, optional=True)
    for name in ("resource_requirements", "constraints", "payload_template"):
        if not isinstance(getattr(spec, name), dict):
            errs.append(f"{name} must be an object")
    if spec.expires_at is not None and not isinstance(spec.expires_at, datetime):
        errs.append("expires_at must be a datetime")
    if not errs and spec.workload_type in cpu_data.CPU_DATA_WORKLOADS and spec.payload_template:
        try:
            cpu_data.validate_payload(spec.workload_type, {"seed": "validate", **spec.payload_template})
        except Exception as exc:
            errs.append(f"payload_template invalid: {str(exc)[:200]}")
    return errs


def check_specs(specs: Any, max_count: Optional[int] = None) -> SpecCheck:
    max_count = max_count or settings.ADAPTER_MAX_OPPORTUNITIES
    if not isinstance(specs, (list, tuple)):
        return SpecCheck([], [{"external_id": None, "errors": [f"discover() returned {type(specs).__name__}, "
                                                                "expected a list"]}])
    accepted: list[OpportunitySpec] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs[:max_count]:
        errs = validate_spec(spec)
        ext = getattr(spec, "external_id", None)
        if not errs and ext in seen:
            errs = ["duplicate external_id"]
        if errs:
            rejected.append({"external_id": ext if isinstance(ext, str) else None, "errors": errs})
            continue
        seen.add(ext)
        accepted.append(spec)
    return SpecCheck(accepted, rejected, max(0, len(specs) - max_count))


# ── certification (spec §127/§128) ──────────────────────────────────────────

def certification_problems(rec: RevenueAdapterRecord, target: str, cert: dict[str, Any]) -> list[str]:
    if target not in CERTIFICATION_STAGES:
        return [f"unknown stage {target}"]
    cur = rec.certification_stage if rec.certification_stage in CERTIFICATION_STAGES else "PROPOSED"
    ci, ti = CERTIFICATION_STAGES.index(cur), CERTIFICATION_STAGES.index(target)
    problems: list[str] = []
    if ti > ci + 1:
        problems.append(f"stages advance one at a time ({cur} -> {CERTIFICATION_STAGES[ci + 1]})")
    if ti > CERTIFICATION_STAGES.index("ECONOMIC_REVIEW"):
        checklist = cert.get("checklist") or {}
        missing = [k for k in CERTIFICATION_CHECKLIST if k not in checklist]
        if missing:
            problems.append("checklist incomplete: " + ", ".join(missing))
        econ = cert.get("economics") or {}
        missing_e = [k for k in ECONOMIC_FIELDS if k not in econ]
        if missing_e:
            problems.append("economic certification incomplete: " + ", ".join(missing_e))
        if checklist.get("requires_custody") is True:
            problems.append("adapters that require FarmOS custody are not certifiable (spec §127)")
        if checklist.get("terms_permit_automation") is False or checklist.get("terms_permit_mobile_devices") is False:
            problems.append("provider terms do not permit automated / mobile participation")
    if rec.simulated and ti > CERTIFICATION_STAGES.index(SIMULATED_MAX_STAGE):
        problems.append("simulated adapters cannot advance beyond SANDBOX")
    if ti > CERTIFICATION_STAGES.index("SANDBOX") and (rec.last_reconcile or {}).get("status") != "MATCHED":
        problems.append("a MATCHED reconciliation is required before CANARY or later")
    return problems
