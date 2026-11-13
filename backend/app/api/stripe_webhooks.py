"""POST /api/v1/webhooks/stripe (spec §104).

Reads the raw body (signature is over exact bytes), verifies the
``Stripe-Signature`` header with STRIPE_WEBHOOK_SECRET, then records +
processes the event idempotently in one transaction. Unauthenticated by design;
the signature is the authentication.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.enums import AuditEventType
from app.db.session import get_db
from app.logging import get_logger
from app.services.developer import stripe_client, stripe_webhooks

router = APIRouter()
logger = get_logger(__name__)
MAX_BODY = 512 * 1024


@router.post("/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db),
                         stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "payload too large")
    try:
        event = stripe_client.verify_and_parse(raw, stripe_signature)
    except stripe_client.SignatureError as exc:
        db.add(AuditLog(event_type=AuditEventType.STRIPE_WEBHOOK_REJECTED, actor_type="SYSTEM",
                        event_metadata={"reason": str(exc)[:200],
                                        "client": request.client.host if request.client else None}))
        await db.commit()
        logger.warning("stripe_webhook_rejected", reason=str(exc))
        raise HTTPException(400, f"invalid signature: {exc}")
    try:
        result = await stripe_webhooks.process_event(db, event, raw)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("stripe_webhook_failed", event_id=event.get("id"), event_type=event.get("type"))
        raise HTTPException(500, "processing failed; Stripe will retry")
    return result
