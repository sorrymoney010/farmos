"""Prepaid customer billing on the double-entry ledger (spec §101-§103, §137).

Money flow (every step is an idempotent, balanced ledger transaction):

  funding   (Stripe webhook only)   CUSTOMER_FUNDING     Dr 1050 Customer Funds Clearing
                                                        Cr 2020 Customer Prepaid Liability[customer]
  reserve   (job created)           no posting - a RESERVED CustomerJobCharge row; available
                                    balance = 2020 balance - open reservations
  charge    (PRIMARY verification)  CUSTOMER_JOB_CHARGE  Dr 2020[customer]  Cr 1020 Revenue Clearing
  revenue   (same step)             JOB_REVENUE_CONFIRMED Dr 1020  Cr 2010 provider / 4045 / 4000|4010
  release   (job failed/expired)    no posting - reservation RELEASED, nothing was charged
  refund    (Stripe charge.refunded) REFUND              Dr 2020[customer]  Cr 1050

Customers are never charged for unverified work, and revenue is only recognised
from these customer charges - never from a frontend "success" redirect.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.developer import Customer, CustomerJobCharge, CustomerTopup
from app.db.models.enums import AuditEventType, JobStatus
from app.db.models.finance import LedgerTransaction
from app.db.models.jobs_ledger import Job, LedgerAccount, LedgerEntry
from app.services.finance import ledger
from app.services.finance.ledger import ZERO, d

# Terminal without verified work. FAILED / INVALID_RESULT are only terminal once
# dead-lettered (otherwise the job is requeued for another attempt).
FAILED_JOB_STATES = (JobStatus.REJECTED, JobStatus.EXPIRED, JobStatus.CANCELLED, JobStatus.PAYMENT_FAILED,
                     JobStatus.QUARANTINED)
RETRYABLE_FAIL_STATES = (JobStatus.FAILED, JobStatus.INVALID_RESULT)
CHARGEABLE_JOB_STATES = (JobStatus.VERIFIED, JobStatus.REVENUE_PENDING, JobStatus.REVENUE_CONFIRMED,
                         JobStatus.SETTLED)


class InsufficientFunds(Exception):
    def __init__(self, available: Decimal, required: Decimal):
        super().__init__(f"insufficient prepaid balance: available {available}, required {required}")
        self.available, self.required = available, required


class CustomerOnHold(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def reserved_total(db: AsyncSession, customer_id: uuid.UUID, book: str) -> Decimal:
    v = (await db.execute(select(func.coalesce(func.sum(CustomerJobCharge.reserved_usd), 0)).where(
        CustomerJobCharge.customer_id == customer_id, CustomerJobCharge.book == book,
        CustomerJobCharge.status == "RESERVED"))).scalar()
    return d(v or 0)


async def balances(db: AsyncSession, customer_id: uuid.UUID, book: str) -> dict[str, Decimal]:
    bal = await ledger.balance(db, "2020", book, customer_id)
    res = await reserved_total(db, customer_id, book)
    return {"balance": bal, "reserved": res, "available": bal - res}


async def balance_dict(db: AsyncSession, customer: Customer, book: str) -> dict[str, Any]:
    from app.config import settings
    b = await balances(db, customer.id, book)
    charged = (await db.execute(select(func.coalesce(func.sum(CustomerJobCharge.charged_usd), 0)).where(
        CustomerJobCharge.customer_id == customer.id, CustomerJobCharge.book == book,
        CustomerJobCharge.status == "CHARGED"))).scalar()
    return {"book": book, "currency": "USD", "balance_usd": str(b["balance"]), "reserved_usd": str(b["reserved"]),
            "available_usd": str(b["available"]), "lifetime_charged_usd": str(d(charged or 0)),
            "low_balance": b["available"] < d(settings.CUSTOMER_LOW_BALANCE_USD),
            "customer_status": customer.status, "test_mode": book == ledger.TEST}


async def reserve(db: AsyncSession, *, customer: Customer, job: Job, deployment_id: Optional[uuid.UUID],
                  run_index: Optional[int], price: Decimal, reserve_amount: Decimal, book: str) -> CustomerJobCharge:
    """Reserve funds for one job. Locks the customer row so concurrent reservations
    cannot overspend the prepaid balance."""
    locked = (await db.execute(select(Customer).where(Customer.id == customer.id).with_for_update())).scalar_one()
    if locked.status != "ACTIVE":
        raise CustomerOnHold(locked.hold_reason or locked.status)
    b = await balances(db, customer.id, book)
    need = d(reserve_amount)
    if b["available"] < need:
        raise InsufficientFunds(b["available"], need)
    ch = CustomerJobCharge(id=uuid.uuid4(), job_id=job.id, deployment_id=deployment_id, customer_id=customer.id,
                           run_index=run_index, book=book, reserved_usd=need, price_usd=d(price),
                           charged_usd=ZERO, status="RESERVED")
    db.add(ch)
    await db.flush()
    return ch


async def charge_for_job(db: AsyncSession, job_id: uuid.UUID) -> Optional[CustomerJobCharge]:
    """Convert a RESERVED charge into a ledger charge (only for verified work)."""
    ch = (await db.execute(select(CustomerJobCharge).where(CustomerJobCharge.job_id == job_id)
                           .with_for_update())).scalar_one_or_none()
    if ch is None or ch.status != "RESERVED":
        return ch
    amount = min(d(ch.price_usd), d(ch.reserved_usd))
    if amount > ZERO:
        tx, _ = await ledger.post(
            db, tx_type="CUSTOMER_JOB_CHARGE", book=ch.book,
            lines=[ledger.dr("2020", amount, ch.customer_id, memo=f"job {job_id}"), ledger.cr("1020", amount)],
            idempotency_key=f"custcharge:{job_id}", reference_type="JOB", reference_id=job_id,
            description="prepaid charge for verified job",
            metadata={"customer_id": str(ch.customer_id), "deployment_id": str(ch.deployment_id or ""),
                      "run_index": ch.run_index, "charge_id": str(ch.id)})
        ch.ledger_transaction_id = tx.id
    ch.charged_usd = amount
    ch.status, ch.settled_at = "CHARGED", utcnow()
    await db.flush()
    return ch


async def on_job_verified(db: AsyncSession, job: Job) -> Optional[dict[str, Any]]:
    """Hook from verification (PRIMARY passed): charge prepaid credit, then confirm
    revenue for that job from the real billing event. Revenue confirmation runs in
    a savepoint so a paused/blocked confirmation never undoes the charge; it is
    retried by the normal confirm-revenue batch."""
    prior = (await db.execute(select(CustomerJobCharge.status).where(CustomerJobCharge.job_id == job.id))).scalar()
    ch = await charge_for_job(db, job.id)
    if ch is None or ch.status != "CHARGED":
        return None
    if prior == "RESERVED":
        from app.services.developer import deployments
        await deployments.enqueue_job_events(db, [job.id], "job.verified")
    from app.services.finance import controls, revenue
    out: dict[str, Any] = {"charge_id": str(ch.id), "charged_usd": str(ch.charged_usd), "revenue": None}
    if await controls.is_paused(db, "PAUSE_REVENUE_CONFIRMATION"):
        out["revenue"] = "paused"
        return out
    try:
        async with db.begin_nested():
            rep = await revenue.confirm_revenue(db, job_ids=[job.id], limit=1)
        out["revenue"] = "confirmed" if rep["confirmed"] else (rep["skipped"] or "pending")
    except Exception as exc:  # noqa: BLE001 - charge stands; revenue retried by batch
        out["revenue"] = f"deferred: {type(exc).__name__}"
    return out


async def settle_open_reservations(db: AsyncSession, limit: int = 500) -> dict[str, int]:
    """Release reservations for jobs that ended without verified work; charge any
    verified job whose hook was missed (crash-safety)."""
    rows = (await db.execute(
        select(CustomerJobCharge, Job.status, Job.dead_lettered).join(Job, Job.id == CustomerJobCharge.job_id)
        .where(CustomerJobCharge.status == "RESERVED",
               or_(Job.status.in_(FAILED_JOB_STATES + CHARGEABLE_JOB_STATES),
                   and_(Job.status.in_(RETRYABLE_FAIL_STATES), Job.dead_lettered.is_(True))))
        .limit(limit).with_for_update(of=CustomerJobCharge, skip_locked=True))).all()
    released = charged = 0
    released_jobs: list[uuid.UUID] = []
    charged_jobs: list[uuid.UUID] = []
    for ch, st, _dl in rows:
        if st not in CHARGEABLE_JOB_STATES:
            ch.status, ch.status_reason, ch.settled_at = "RELEASED", f"job {st.value.lower()}", utcnow()
            released += 1
            released_jobs.append(ch.job_id)
        else:
            await charge_for_job(db, ch.job_id)
            charged += 1
            charged_jobs.append(ch.job_id)
    await db.flush()
    from app.services.developer import deployments
    await deployments.enqueue_job_events(db, released_jobs, "job.failed")
    await deployments.enqueue_job_events(db, charged_jobs, "job.verified")
    return {"released": released, "charged": charged}


async def release_for_job(db: AsyncSession, job_id: uuid.UUID, reason: str) -> None:
    ch = (await db.execute(select(CustomerJobCharge).where(CustomerJobCharge.job_id == job_id)
                           .with_for_update())).scalar_one_or_none()
    if ch is not None and ch.status == "RESERVED":
        ch.status, ch.status_reason, ch.settled_at = "RELEASED", reason, utcnow()


# ── funding / refunds (called only from verified Stripe webhooks) ───────────

async def fund_topup(db: AsyncSession, topup: CustomerTopup, *, source_event: str) -> LedgerTransaction:
    amount = d(Decimal(topup.amount_cents) / 100)
    tx, created = await ledger.post(
        db, tx_type="CUSTOMER_FUNDING", book=topup.book,
        lines=[ledger.dr("1050", amount), ledger.cr("2020", amount, topup.customer_id)],
        idempotency_key=f"funding:{topup.id}", reference_type="CUSTOMER_TOPUP", reference_id=topup.id,
        description="prepaid credit top-up (Stripe)",
        metadata={"customer_id": str(topup.customer_id), "stripe_event_id": source_event,
                  "checkout_session": topup.stripe_checkout_session_id,
                  "payment_intent": topup.stripe_payment_intent_id, "livemode": topup.livemode})
    if topup.status != "SUCCEEDED":
        topup.status, topup.funded_usd, topup.funded_at = "SUCCEEDED", amount, utcnow()
        topup.ledger_transaction_id = tx.id
    if created:
        db.add(AuditLog(event_type=AuditEventType.CUSTOMER_FUNDED, actor_type="SYSTEM",
                        target_type="CUSTOMER", target_id=topup.customer_id,
                        event_metadata={"topup_id": str(topup.id), "amount_usd": str(amount), "book": topup.book,
                                        "stripe_event_id": source_event}))
    return tx


async def refund_topup(db: AsyncSession, topup: CustomerTopup, *, refunded_total_usd: Decimal,
                       source_event: str) -> Optional[LedgerTransaction]:
    """Stripe reports the cumulative refunded amount; post only the delta."""
    if topup.status not in ("SUCCEEDED", "PARTIALLY_REFUNDED"):
        return None
    already = d(topup.refunded_usd or 0)
    target = min(d(refunded_total_usd), d(topup.funded_usd or 0))
    delta = target - already
    if delta <= ZERO:
        return None
    tx, created = await ledger.post(
        db, tx_type="REFUND", book=topup.book,
        lines=[ledger.dr("2020", delta, topup.customer_id), ledger.cr("1050", delta)],
        idempotency_key=f"refund:{topup.id}:{target}", reference_type="CUSTOMER_TOPUP", reference_id=topup.id,
        description="prepaid credit refunded (Stripe)",
        metadata={"customer_id": str(topup.customer_id), "stripe_event_id": source_event,
                  "refunded_total": str(target)})
    topup.refunded_usd = target
    topup.status = "REFUNDED" if target >= d(topup.funded_usd or 0) else "PARTIALLY_REFUNDED"
    if created:
        db.add(AuditLog(event_type=AuditEventType.CUSTOMER_REFUNDED, actor_type="SYSTEM",
                        target_type="CUSTOMER", target_id=topup.customer_id,
                        event_metadata={"topup_id": str(topup.id), "delta_usd": str(delta),
                                        "stripe_event_id": source_event}))
    return tx


async def customer_ledger(db: AsyncSession, customer_id: uuid.UUID, book: str, limit: int = 100) -> list[dict]:
    rows = (await db.execute(
        select(LedgerTransaction, LedgerEntry).join(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerAccount.code == "2020", LedgerAccount.owner_id == customer_id, LedgerAccount.book == book)
        .order_by(LedgerTransaction.posted_at.desc()).limit(limit))).all()
    out = []
    for tx, e in rows:
        sign = 1 if e.direction.value == "CREDIT" else -1
        out.append({"transaction_id": str(tx.id), "type": tx.tx_type, "book": tx.book,
                    "amount_usd": str(d(e.amount) * sign), "description": tx.description,
                    "reference_type": tx.reference_type,
                    "reference_id": str(tx.reference_id) if tx.reference_id else None,
                    "created_at": tx.posted_at.isoformat() if tx.posted_at else None})
    return out


async def reconcile(db: AsyncSession, book: str) -> dict[str, Any]:
    """Spec §137: Stripe funding records vs ledger, and prepaid liability vs charges."""
    topups = (await db.execute(select(CustomerTopup).where(
        CustomerTopup.book == book, CustomerTopup.status.in_(("SUCCEEDED", "PARTIALLY_REFUNDED", "REFUNDED")))
    )).scalars().all()
    mismatches = []
    stripe_total = ZERO
    for t in topups:
        stripe_total += d(t.funded_usd or 0)
        tx = (await db.execute(select(LedgerTransaction).where(
            LedgerTransaction.idempotency_key == f"funding:{t.id}"))).scalar_one_or_none()
        if tx is None:
            mismatches.append({"topup_id": str(t.id), "issue": "succeeded top-up without ledger funding"})
    funded_tx = (await db.execute(select(LedgerTransaction).where(
        LedgerTransaction.book == book, LedgerTransaction.tx_type == "CUSTOMER_FUNDING",
        LedgerTransaction.reference_type == "CUSTOMER_TOPUP"))).scalars().all()
    known = {str(t.id) for t in topups}
    for tx in funded_tx:
        if str(tx.reference_id) not in known:
            mismatches.append({"transaction_id": str(tx.id), "issue": "ledger funding without succeeded top-up"})
    liability = await ledger.balance(db, "2020", book)
    funded = sum((d(t.funded_usd or 0) for t in topups), ZERO)
    refunded = sum((d(t.refunded_usd or 0) for t in topups), ZERO)
    charged = d((await db.execute(select(func.coalesce(func.sum(CustomerJobCharge.charged_usd), 0)).where(
        CustomerJobCharge.book == book, CustomerJobCharge.status == "CHARGED"))).scalar() or 0)
    expected = funded - refunded - charged
    if expected != liability:
        mismatches.append({"issue": "prepaid liability drift", "ledger_2020": str(liability),
                           "expected": str(expected)})
    return {"book": book, "ok": not mismatches, "succeeded_topups": len(topups),
            "stripe_funded_usd": str(stripe_total), "refunded_usd": str(refunded), "charged_usd": str(charged),
            "prepaid_liability_usd": str(liability), "unbalanced_transactions": await ledger.unbalanced_transactions(db, book),
            "mismatches": mismatches}
