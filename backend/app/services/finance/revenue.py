"""Revenue confirmation + provider split (spec §23, §28, §29, §66, §151).

UNCONFIRMED (internal) / SIMULATED (simulated adapter) job revenue from
verified jobs becomes CONFIRMED only when a billing source confirms it. Only
the simulated billing source exists, so every posting lands on the TEST book.

Confirmed revenue posts (spec §66):
    Dr 1020/1030 Revenue Clearing     gross
    Cr 2010 Provider Pending (prov)   provider share
    Cr 4045 Infrastructure Recovery   infra share
    Cr 4000/4010/4030 Revenue         FARMOS share (remainder; never rounding loss)
then the simulated settlement (Dr 1000 Treasury / Cr clearing) plus a matching
deposit on the simulated chain. After PROVIDER_EARNINGS_HOLD_HOURS the provider
share moves PENDING -> AVAILABLE (Dr 2010 / Cr 2000).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import registry
from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.core import Provider, SystemSettings
from app.db.models.devices import Device
from app.db.models.enums import AuditEventType
from app.db.models.jobs_ledger import Job, JobEconomics
from app.services.finance import controls, ledger
from app.services.finance.controls import FinanceError
from app.services.finance.ledger import ZERO, d

SPLIT_KEY = "revenue_split_policy"
SPLIT_KEYS = ("provider", "infrastructure", "farmos")
MONITORING = {"http_check", "api_request", "monitor"}
DISPUTED_JOB_IDS: set[str] = set()  # test hook for the simulated billing source


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── split policy (spec §29: configurable per adapter/workload, never hard-coded) ──

def validate_split(split: dict[str, Any]) -> dict[str, float]:
    if set(split) != set(SPLIT_KEYS):
        raise ValueError(f"split must have exactly {', '.join(SPLIT_KEYS)}")
    vals = {k: float(split[k]) for k in SPLIT_KEYS}
    if any(v < 0 for v in vals.values()):
        raise ValueError("split shares must be >= 0")
    if abs(sum(vals.values()) - 1.0) > 1e-9:
        raise ValueError("split shares must sum to 1")
    return vals


async def split_policy(db: AsyncSession) -> dict[str, Any]:
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == SPLIT_KEY))).scalar_one_or_none()
    over = dict(row.value or {}) if row else {}
    return {"default": validate_split(over.get("default") or settings.REVENUE_SPLIT_DEFAULT),
            "overrides": {k: validate_split(v) for k, v in {**settings.REVENUE_SPLIT_OVERRIDES,
                                                             **(over.get("overrides") or {})}.items()}}


async def set_split_policy(db: AsyncSession, value: dict[str, Any], actor_id: uuid.UUID | None) -> dict[str, Any]:
    clean = {"default": validate_split(value.get("default") or settings.REVENUE_SPLIT_DEFAULT),
             "overrides": {k: validate_split(v) for k, v in (value.get("overrides") or {}).items()}}
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == SPLIT_KEY))).scalar_one_or_none()
    if row is None:
        db.add(SystemSettings(key=SPLIT_KEY, value=clean, description="Revenue split policy", updated_by=actor_id))
    else:
        row.value, row.updated_by = clean, actor_id
    db.add(AuditLog(event_type=AuditEventType.SETTINGS_CHANGED, actor_type="ADMIN", actor_id=actor_id,
                    target_type="SETTING", event_metadata={"key": SPLIT_KEY, "value": clean}))
    return clean


def split_for(policy: dict[str, Any], adapter_id: Optional[str], workload_type: Optional[str]) -> dict[str, float]:
    o = policy["overrides"]
    return o.get(adapter_id or "") or o.get(workload_type or "") or policy["default"]


def split_amounts(gross: Decimal, split: dict[str, float]) -> tuple[Decimal, Decimal, Decimal]:
    gross = d(gross)
    provider = d(gross * Decimal(str(split["provider"])))
    infra = d(gross * Decimal(str(split["infrastructure"])))
    return provider, infra, gross - provider - infra


def is_external(adapter_id: Optional[str]) -> bool:
    a = registry.get(adapter_id or "")
    return bool(a and a.kind == "EXTERNAL")


def revenue_code(adapter_id: Optional[str], workload_type: Optional[str]) -> str:
    if is_external(adapter_id):
        return "4030"
    return "4010" if workload_type in MONITORING else "4000"


# ── billing source ───────────────────────────────────────────────────────────

@dataclass
class BillingConfirmation:
    status: str          # CONFIRMED | DISPUTED
    amount_usd: Decimal
    reference: str


class SimulatedBillingSource:
    """Test-only billing source: confirms verified work at its measured value.
    Never contacts Stripe or any payment network; postings go to the TEST book."""
    name = "simulated_billing"
    book = ledger.TEST

    def confirm(self, row: JobEconomics) -> BillingConfirmation:
        ref = "SIMBILL-" + hashlib.sha256(str(row.job_id).encode()).hexdigest()[:16]
        if str(row.job_id) in DISPUTED_JOB_IDS:
            return BillingConfirmation("DISPUTED", ZERO, ref)
        return BillingConfirmation("CONFIRMED", d(row.actual_gross_usd or 0), ref)


def billing_source() -> SimulatedBillingSource:
    if settings.BILLING_SOURCE != "simulated":
        raise RuntimeError("only the simulated billing source exists in this build")
    return SimulatedBillingSource()


# ── confirmation + release ───────────────────────────────────────────────────

async def confirm_revenue(db: AsyncSession, *, job_ids: list | None = None, limit: int = 200,
                          actor_id: uuid.UUID | None = None, now: datetime | None = None) -> dict[str, Any]:
    from app.payouts import get_provider

    now = now or utcnow()
    if await controls.is_paused(db, "PAUSE_REVENUE_CONFIRMATION"):
        raise FinanceError("revenue confirmation is paused (kill switch)")
    source = billing_source()
    policy = await split_policy(db)
    q = (select(JobEconomics).where(JobEconomics.status == "ACTUAL",
                                    JobEconomics.revenue_state.in_(("UNCONFIRMED", "SIMULATED")),
                                    JobEconomics.confirmed_at.is_(None))
         .order_by(JobEconomics.finalized_at).limit(limit).with_for_update(skip_locked=True))
    if job_ids:
        q = q.where(JobEconomics.job_id.in_([uuid.UUID(str(j)) for j in job_ids]))
    rows = (await db.execute(q)).scalars().all()
    report: dict[str, Any] = {"confirmed": [], "disputed": [], "skipped": {}, "book": source.book,
                              "source": source.name}
    chain = get_provider()
    from app.services import opportunity_service as opps
    adapter_blocks: dict[str, Any] = {}
    for row in rows:
        jid = str(row.job_id)
        block = await opps.earnings_release_block(db, row.adapter_id, adapter_blocks)
        if block:  # spec §126: unreconciled external revenue stays unconfirmed
            report["skipped"][jid] = block
            continue
        device = (await db.execute(select(Device).where(Device.id == row.device_id))).scalar_one_or_none() \
            if row.device_id else None
        if device is None:
            report["skipped"][jid] = "no device"
            continue
        job = (await db.execute(select(Job).where(Job.id == row.job_id))).scalar_one()
        if (job.constraints or {}).get("pilot_mode"):
            report["skipped"][jid] = "unfunded device pilot; not revenue"
            continue
        conf = source.confirm(row)
        row.confirmation_source, row.confirmation_ref, row.ledger_book = source.name, conf.reference, source.book
        if conf.status == "DISPUTED":
            row.revenue_state = "DISPUTED"
            report["disputed"].append(jid)
            continue
        gross = d(conf.amount_usd)
        split = split_for(policy, row.adapter_id, job.workload_type)
        p_amt, infra, farmos = split_amounts(gross, split)
        tx = None
        if gross > ZERO:
            clearing = "1030" if is_external(row.adapter_id) else "1020"
            lines = [ledger.dr(clearing, gross)]
            if p_amt > ZERO:
                lines.append(ledger.cr("2010", p_amt, device.provider_id, memo=f"device {device.human_id}"))
            if infra > ZERO:
                lines.append(ledger.cr("4045", infra))
            if farmos > ZERO:
                lines.append(ledger.cr(revenue_code(row.adapter_id, job.workload_type), farmos))
            meta = {"job_id": jid, "adapter_id": row.adapter_id, "workload_type": job.workload_type,
                    "split": split, "source": source.name, "reference": conf.reference,
                    "device_id": str(device.id), "prior_state": row.revenue_state}
            tx, _ = await ledger.post(db, tx_type="JOB_REVENUE_CONFIRMED", book=source.book, lines=lines,
                                      idempotency_key=f"revenue:{jid}", reference_type="JOB", reference_id=row.job_id,
                                      description=f"confirmed {job.workload_type} revenue", actor_id=actor_id,
                                      actor_type="ADMIN" if actor_id else "SYSTEM", metadata=meta)
            # Simulated settlement: the (simulated) customer payment / token conversion reaches treasury.
            settle_type = "TOKEN_CONVERSION" if clearing == "1030" else "CUSTOMER_JOB_CHARGE"
            await ledger.post(db, tx_type=settle_type, book=source.book,
                              lines=[ledger.dr("1000", gross), ledger.cr(clearing, gross)],
                              idempotency_key=f"settle:{jid}", reference_type="JOB", reference_id=row.job_id,
                              description="simulated billing settlement", metadata={"reference": conf.reference})
            await chain.deposit(db, settings.PAYOUT_HOT_WALLET_ADDRESS, "USDC", gross, f"settle:{jid}")
        row.revenue_state = "CONFIRMED"
        row.confirmed_at = now
        row.confirmed_gross_usd = gross
        row.provider_id = device.provider_id
        row.provider_share_usd = p_amt
        row.revenue_transaction_id = tx.id if tx else None
        if p_amt <= ZERO:
            row.earnings_available_at = now
        db.add(AuditLog(event_type=AuditEventType.REVENUE_CONFIRMED, actor_type="ADMIN" if actor_id else "SYSTEM",
                        actor_id=actor_id, target_type="JOB", target_id=row.job_id,
                        event_metadata={"gross": str(gross), "provider_share": str(p_amt), "book": source.book,
                                        "reference": conf.reference}))
        report["confirmed"].append({"job_id": jid, "gross": str(gross), "provider_share": str(p_amt),
                                    "infrastructure": str(infra), "farmos": str(farmos),
                                    "provider_id": str(device.provider_id)})
    await db.flush()
    return report


async def release_pending(db: AsyncSession, *, now: datetime | None = None, limit: int = 500,
                          actor_id: uuid.UUID | None = None) -> dict[str, Any]:
    """PENDING -> AVAILABLE once the confirmation hold has passed (spec §23, §151).
    Providers on payout hold or with an OPEN risk event stay pending."""
    now = now or utcnow()
    cutoff = now - timedelta(hours=settings.PROVIDER_EARNINGS_HOLD_HOURS)
    rows = (await db.execute(
        select(JobEconomics).where(JobEconomics.revenue_state == "CONFIRMED",
                                   JobEconomics.earnings_available_at.is_(None),
                                   JobEconomics.confirmed_at <= cutoff, JobEconomics.provider_id.is_not(None))
        .order_by(JobEconomics.confirmed_at).limit(limit).with_for_update(skip_locked=True))).scalars().all()
    report: dict[str, Any] = {"released": [], "held": {}}
    from app.services import opportunity_service as opps
    adapter_blocks: dict[str, Any] = {}
    for row in rows:
        block = await opps.earnings_release_block(db, row.adapter_id, adapter_blocks)
        if block:  # spec §126: do not convert external estimates/pending to AVAILABLE
            report["held"][str(row.job_id)] = block
            continue
        prov = (await db.execute(select(Provider).where(Provider.id == row.provider_id))).scalar_one_or_none()
        if prov is None or prov.payout_hold:
            report["held"][str(row.job_id)] = "provider payout hold"
            continue
        if await controls.has_open_risk(db, "PROVIDER", row.provider_id):
            report["held"][str(row.job_id)] = "open risk event on provider"
            continue
        amt = d(row.provider_share_usd or 0)
        await ledger.post(db, tx_type="PROVIDER_EARNING_AVAILABLE", book=row.ledger_book or ledger.TEST,
                          lines=[ledger.dr("2010", amt, row.provider_id), ledger.cr("2000", amt, row.provider_id)],
                          idempotency_key=f"available:{row.job_id}", reference_type="JOB", reference_id=row.job_id,
                          description="provider earnings available", actor_id=actor_id,
                          actor_type="ADMIN" if actor_id else "SYSTEM")
        row.earnings_available_at = now
        report["released"].append({"job_id": str(row.job_id), "amount": str(amt), "provider_id": str(row.provider_id)})
    await db.flush()
    return report
