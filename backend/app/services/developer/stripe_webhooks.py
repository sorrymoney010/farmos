"""Stripe webhook processing (spec §101, §104, §154).

The HTTP layer verifies the signature over the *raw* body first; this module then:
1. inserts a ``stripe_webhook_events`` row keyed by the unique Stripe event id
   (a duplicate delivery hits the unique constraint and is acknowledged without
   re-processing -> exactly one credit per event);
2. processes the event inside the same DB transaction (savepoint per event), so
   the event record, top-up state and ledger posting commit atomically;
3. never trusts metadata alone: the top-up must exist, belong to the customer,
   and amount / currency / livemode must match what FARMOS created.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.developer import Customer, CustomerTopup, StripeWebhookEvent
from app.db.models.enums import AuditEventType
from app.db.models.finance import RiskEvent
from app.services.developer import billing, stripe_client
from app.services.finance import ledger

CREDIT_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded",
                 "payment_intent.succeeded"}
FAIL_EVENTS = {"checkout.session.expired": "EXPIRED", "checkout.session.async_payment_failed": "FAILED",
               "payment_intent.payment_failed": "FAILED", "payment_intent.canceled": "CANCELLED"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(v: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None


async def _find_topup(db: AsyncSession, obj: dict[str, Any]) -> Optional[CustomerTopup]:
    meta = obj.get("metadata") or {}
    tid = _uuid(meta.get("farmos_topup_id") or obj.get("client_reference_id"))
    conds = []
    if tid:
        conds.append(CustomerTopup.id == tid)
    oid = obj.get("id")
    if obj.get("object") == "checkout.session" and oid:
        conds.append(CustomerTopup.stripe_checkout_session_id == oid)
    pi = obj.get("payment_intent") if obj.get("object") != "payment_intent" else oid
    if isinstance(pi, dict):
        pi = pi.get("id")
    if pi:
        conds.append(CustomerTopup.stripe_payment_intent_id == pi)
    if not conds:
        return None
    return (await db.execute(select(CustomerTopup).where(or_(*conds)).with_for_update())).scalars().first()


def _verify_amount(topup: CustomerTopup, obj: dict[str, Any], livemode: bool) -> Optional[str]:
    amount = obj.get("amount_total") if obj.get("object") == "checkout.session" else \
        obj.get("amount_received", obj.get("amount"))
    if amount is None or int(amount) != int(topup.amount_cents):
        return f"amount mismatch: stripe={amount} expected={topup.amount_cents}"
    if str(obj.get("currency", "")).lower() != topup.currency.lower():
        return f"currency mismatch: {obj.get('currency')}"
    if bool(livemode) != bool(topup.livemode):
        return "livemode mismatch"
    meta_cust = (obj.get("metadata") or {}).get("farmos_customer_id")
    if meta_cust and str(meta_cust) != str(topup.customer_id):
        return "customer mismatch"
    return None


async def _handle(db: AsyncSession, ev: StripeWebhookEvent, event: dict[str, Any]) -> tuple[str, Optional[str]]:
    etype = event["type"]
    obj = (event.get("data") or {}).get("object") or {}
    livemode = bool(event.get("livemode"))
    if livemode and stripe_client.mode() != "live":
        return "REJECTED", "live event received while live mode is disabled"

    if etype in CREDIT_EVENTS:
        if obj.get("object") == "checkout.session" and obj.get("payment_status") != "paid":
            return "IGNORED", f"payment_status={obj.get('payment_status')} (await async_payment_succeeded)"
        topup = await _find_topup(db, obj)
        if topup is None:
            return "IGNORED", "no matching FARMOS top-up"
        ev.related_topup_id, ev.related_customer_id = topup.id, topup.customer_id
        err = _verify_amount(topup, obj, livemode)
        if err:
            db.add(RiskEvent(category="STRIPE_MISMATCH", severity="HIGH", subject_type="CUSTOMER_TOPUP",
                             subject_id=topup.id, message=err, details={"stripe_event_id": event["id"]}))
            return "REJECTED", err
        if obj.get("object") == "payment_intent" and not topup.stripe_payment_intent_id:
            topup.stripe_payment_intent_id = obj.get("id")
        if obj.get("object") == "checkout.session" and isinstance(obj.get("payment_intent"), str) \
                and not topup.stripe_payment_intent_id:
            topup.stripe_payment_intent_id = obj["payment_intent"]
        if topup.status in ("SUCCEEDED", "PARTIALLY_REFUNDED", "REFUNDED"):
            return "DUPLICATE", "top-up already funded"  # e.g. session.completed + payment_intent.succeeded
        await billing.fund_topup(db, topup, source_event=event["id"])
        return "PROCESSED", f"credited {topup.amount_cents} cents"

    if etype in FAIL_EVENTS:
        topup = await _find_topup(db, obj)
        if topup is None:
            return "IGNORED", "no matching FARMOS top-up"
        ev.related_topup_id, ev.related_customer_id = topup.id, topup.customer_id
        if topup.status in ("PENDING", "CREATED"):
            topup.status = FAIL_EVENTS[etype]
        return "PROCESSED", f"top-up {topup.status.lower()}"

    if etype == "charge.refunded":
        topup = await _find_topup(db, obj)
        if topup is None:
            return "IGNORED", "no matching FARMOS top-up"
        ev.related_topup_id, ev.related_customer_id = topup.id, topup.customer_id
        total = ledger.d(Decimal(int(obj.get("amount_refunded") or 0)) / 100)
        tx = await billing.refund_topup(db, topup, refunded_total_usd=total, source_event=event["id"])
        return "PROCESSED", "refund posted" if tx else "nothing new to refund"

    if etype == "charge.dispute.created":
        pi = obj.get("payment_intent")
        topup = await _find_topup(db, {"object": "charge", "payment_intent": pi, "metadata": obj.get("metadata")})
        if topup is None:
            return "IGNORED", "no matching FARMOS top-up"
        ev.related_topup_id, ev.related_customer_id = topup.id, topup.customer_id
        cust = (await db.execute(select(Customer).where(Customer.id == topup.customer_id)
                                 .with_for_update())).scalar_one()
        cust.status, cust.hold_reason = "ON_HOLD", f"Stripe dispute {obj.get('id')}"
        db.add(RiskEvent(category="CHARGEBACK", severity="HIGH", subject_type="CUSTOMER", subject_id=cust.id,
                         message="card dispute opened; customer placed on hold",
                         details={"stripe_event_id": event["id"], "dispute": obj.get("id"),
                                  "amount": obj.get("amount")}))
        db.add(AuditLog(event_type=AuditEventType.CUSTOMER_ON_HOLD, actor_type="SYSTEM",
                        target_type="CUSTOMER", target_id=cust.id, event_metadata={"reason": cust.hold_reason}))
        return "PROCESSED", "customer on hold (dispute)"

    return "IGNORED", f"unhandled event type {etype}"


async def process_event(db: AsyncSession, event: dict[str, Any], raw: bytes) -> dict[str, Any]:
    """Idempotently record + process one verified event. Caller commits."""
    payload_hash = hashlib.sha256(raw).hexdigest()
    ev = StripeWebhookEvent(id=uuid.uuid4(), stripe_event_id=str(event["id"])[:255],
                            event_type=str(event["type"])[:100], livemode=bool(event.get("livemode")),
                            processing_status="RECEIVED", payload_hash=payload_hash)
    try:
        async with db.begin_nested():
            db.add(ev)
            await db.flush()
    except IntegrityError:
        prior = (await db.execute(select(StripeWebhookEvent).where(
            StripeWebhookEvent.stripe_event_id == str(event["id"])))).scalar_one_or_none()
        return {"received": True, "duplicate": True,
                "status": prior.processing_status if prior else "UNKNOWN"}
    try:
        async with db.begin_nested():
            status, detail = await _handle(db, ev, event)
    except Exception as exc:  # noqa: BLE001 - recorded; Stripe will retry on 500
        ev.processing_status, ev.detail, ev.processed_at = "FAILED", f"{type(exc).__name__}: {exc}"[:2000], utcnow()
        await db.flush()
        raise
    ev.processing_status, ev.detail, ev.processed_at = status, (detail or "")[:2000], utcnow()
    await db.flush()
    return {"received": True, "duplicate": False, "status": status, "detail": detail}
