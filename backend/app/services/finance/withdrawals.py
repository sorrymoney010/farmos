"""Controlled withdrawals (spec §24, §27, §74, §106, §149).

REQUESTED -> funds moved AVAILABLE -> HELD (ledger) -> SECURITY_HOLD (or
RISK_REVIEW on manual review) -> admin APPROVED (two approvers for large
amounts; never by the requester unless explicitly allowed) -> payout batch ->
BROADCAST -> CONFIRMING -> CONFIRMED (ledger settled).
Reject / cancel / payout failure releases HELD -> AVAILABLE.

Only the TEST book can be withdrawn in this build; REAL USDC withdrawals are
refused. Every state change is audited and every money movement is a typed,
balanced, idempotent ledger transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.core import Provider, Wallet
from app.db.models.enums import AuditEventType, ProviderStatus, WithdrawalStatus as WS
from app.db.models.jobs_ledger import Withdrawal
from app.payouts.base import approval_hash
from app.services.finance import controls, ledger
from app.services.finance.controls import FinanceError
from app.services.finance.ledger import d

OPEN = (WS.REQUESTED, WS.SECURITY_HOLD, WS.RISK_REVIEW, WS.APPROVED, WS.BROADCAST, WS.CONFIRMING)
CANCELLABLE = (WS.REQUESTED, WS.SECURITY_HOLD, WS.RISK_REVIEW, WS.APPROVED)
ASSET = "USDC"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def token_for(network: str) -> Optional[dict]:
    t = settings.TOKEN_REGISTRY.get(ASSET, {}).get(network)
    return t if t and t.get("status") == "ACTIVE" else None


def to_atomic(amount: Decimal, decimals: int = 6) -> int:
    q = Decimal(1).scaleb(-decimals)
    a = Decimal(str(amount))
    if a != a.quantize(q, rounding=ROUND_DOWN):
        raise FinanceError(f"amount supports at most {decimals} decimals", 400)
    return int(a.scaleb(decimals))


def evaluate_risk(*, wallet_changed_recently: bool, amount: float, normal_30d: Optional[float],
                  account_age_days: float, auth_anomaly: bool, unresolved_risk: bool) -> tuple[str, float, list[str]]:
    """Spec §74 withdrawal risk (pure). ``normal_30d`` is None without history."""
    score, reasons = 0.0, []
    if wallet_changed_recently:
        score += 0.30
        reasons.append("payout wallet changed recently")
    if normal_30d is not None and amount > normal_30d * 3:
        score += 0.20
        reasons.append("amount > 3x normal 30d withdrawal")
    if account_age_days < 7:
        score += 0.10
        reasons.append("account younger than 7 days")
    if auth_anomaly:
        score += 0.20
        reasons.append("recent auth anomaly")
    if unresolved_risk:
        score += 0.30
        reasons.append("unresolved risk event")
    score = round(score, 4)
    if score >= 0.60:
        return "MANUAL_REVIEW", score, reasons
    if score >= 0.30:
        return "EXTENDED_HOLD", score, reasons
    return "STANDARD_HOLD", score, reasons


async def _release(db: AsyncSession, w: Withdrawal, reason: str, actor_id, actor_type: str) -> None:
    amt = d(w.amount)
    tx, _ = await ledger.post(db, tx_type="WITHDRAWAL_RELEASE", book=w.book,
                              lines=[ledger.dr("2040", amt, w.provider_id), ledger.cr("2000", amt, w.provider_id)],
                              idempotency_key=f"withdrawal-release:{w.id}", reference_type="WITHDRAWAL",
                              reference_id=w.id, description=f"release hold: {reason}"[:500],
                              actor_type=actor_type, actor_id=actor_id)
    w.release_transaction_id = tx.id


async def request(db: AsyncSession, *, user, wallet_id: uuid.UUID, amount: Any, book: str,
                  idempotency_key: str | None, now: datetime | None = None) -> tuple[Withdrawal, bool]:
    now = now or utcnow()
    if not idempotency_key:
        raise FinanceError("Idempotency-Key header is required", 400)
    provider = (await db.execute(select(Provider).where(Provider.user_id == user.id))).scalars().first()
    if provider is None:
        raise FinanceError("no provider account", 403)
    amount = d(amount)
    prior = (await db.execute(select(Withdrawal).where(Withdrawal.provider_id == provider.id,
                                                       Withdrawal.idempotency_key == idempotency_key))).scalar_one_or_none()
    if prior is not None:
        if d(prior.amount) != amount or prior.wallet_id != wallet_id or prior.book != book:
            raise FinanceError("Idempotency-Key reused with different parameters", 409)
        return prior, True
    if await controls.is_paused(db, "PAUSE_WITHDRAWALS"):
        raise FinanceError("withdrawals are paused (kill switch)")
    if book not in ledger.BOOKS:
        raise FinanceError("book must be TEST or REAL", 400)
    if book == ledger.REAL and not settings.WITHDRAWAL_REAL_BOOK_ENABLED:
        raise FinanceError("REAL USDC withdrawals are disabled in this build (no real funds)")
    if provider.status != ProviderStatus.ACTIVE or provider.payout_hold:
        raise FinanceError("provider account is not eligible for payouts (status or payout hold)", 403)
    wallet = (await db.execute(select(Wallet).where(Wallet.id == wallet_id))).scalar_one_or_none()
    if wallet is None or wallet.provider_id != provider.id:
        raise FinanceError("wallet not found", 404)
    if not wallet.is_verified:
        raise FinanceError("wallet ownership is not verified")
    chain_id = settings.PAYOUT_NETWORKS.get(wallet.network)
    token = token_for(wallet.network)
    if chain_id is None or token is None:
        raise FinanceError(f"payouts are not enabled on {wallet.network} in this build")
    atomic = to_atomic(amount, token["decimals"])
    if amount < d(settings.WITHDRAWAL_MIN_USD):
        raise FinanceError(f"minimum withdrawal is {settings.WITHDRAWAL_MIN_USD}", 400)
    if amount > d(settings.WITHDRAWAL_MAX_PER_TX_USD):
        raise FinanceError(f"maximum per withdrawal is {settings.WITHDRAWAL_MAX_PER_TX_USD}", 400)
    age_days = (now - provider.created_at).total_seconds() / 86400 if provider.created_at else 0.0
    limit = settings.WITHDRAWAL_NEW_ACCOUNT_DAILY_LIMIT_USD if age_days < settings.WITHDRAWAL_NEW_ACCOUNT_DAYS \
        else settings.WITHDRAWAL_DAILY_LIMIT_USD
    day_total = d((await db.execute(select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
        Withdrawal.provider_id == provider.id, Withdrawal.book == book,
        Withdrawal.status.in_(OPEN + (WS.CONFIRMED,)), Withdrawal.requested_at >= now - timedelta(hours=24)))).scalar())
    if day_total + amount > d(limit):
        raise FinanceError(f"daily withdrawal limit {limit} exceeded", 409)

    bal = await ledger.provider_balance_row(db, provider.id, book, lock=True)
    if d(bal.available_balance) < amount:
        raise FinanceError(f"insufficient available balance ({d(bal.available_balance)})", 409)

    hist = (await db.execute(select(func.avg(Withdrawal.amount)).where(
        Withdrawal.provider_id == provider.id, Withdrawal.status == WS.CONFIRMED,
        Withdrawal.requested_at >= now - timedelta(days=30)))).scalar()
    decision, score, reasons = evaluate_risk(
        wallet_changed_recently=bool(wallet.cooldown_until and wallet.cooldown_until > now),
        amount=float(amount), normal_30d=float(hist) if hist is not None else None, account_age_days=age_days,
        auth_anomaly=False, unresolved_risk=await controls.has_open_risk(db, "PROVIDER", provider.id))
    hold_h = settings.WITHDRAWAL_EXTENDED_HOLD_HOURS if decision != "STANDARD_HOLD" else settings.WITHDRAWAL_STANDARD_HOLD_HOURS
    eligible = now + timedelta(hours=hold_h)
    if wallet.cooldown_until and wallet.cooldown_until > eligible:
        eligible = wallet.cooldown_until

    w = Withdrawal(id=uuid.uuid4(), provider_id=provider.id, wallet_id=wallet.id, asset=ASSET, network=wallet.network,
                   amount=amount, status=WS.REQUESTED, risk_score=score, requested_at=now, eligible_at=eligible,
                   book=book, requested_by=user.id, idempotency_key=idempotency_key, recipient_address=wallet.address,
                   chain_id=chain_id, amount_atomic=atomic, risk_decision=decision, risk_reasons=reasons, approvals=[],
                   required_approvals=2 if amount >= d(settings.WITHDRAWAL_LARGE_USD) else 1)
    try:
        async with db.begin_nested():
            db.add(w)
            await db.flush()
    except IntegrityError:
        prior = (await db.execute(select(Withdrawal).where(Withdrawal.provider_id == provider.id,
                                                           Withdrawal.idempotency_key == idempotency_key))).scalar_one()
        return prior, True
    tx, _ = await ledger.post(db, tx_type="PROVIDER_WITHDRAWAL_HOLD", book=book,
                              lines=[ledger.dr("2000", amount, provider.id), ledger.cr("2040", amount, provider.id)],
                              idempotency_key=f"withdrawal-hold:{w.id}", reference_type="WITHDRAWAL", reference_id=w.id,
                              description="withdrawal requested: funds held", actor_type="USER", actor_id=user.id)
    w.hold_transaction_id = tx.id
    w.status = WS.RISK_REVIEW if decision == "MANUAL_REVIEW" else WS.SECURITY_HOLD
    if decision == "MANUAL_REVIEW":
        await controls.raise_risk(db, "WITHDRAWAL_REVIEW", f"withdrawal {w.id} needs manual review",
                                  severity="HIGH", subject_type="WITHDRAWAL", subject_id=w.id,
                                  details={"score": score, "reasons": reasons})
    db.add(AuditLog(event_type=AuditEventType.WITHDRAWAL_REQUESTED, actor_type="USER", actor_id=user.id,
                    target_type="WITHDRAWAL", target_id=w.id,
                    event_metadata={"amount": str(amount), "book": book, "decision": decision, "score": score,
                                    "recipient": wallet.address, "eligible_at": eligible.isoformat()}))
    await db.flush()
    return w, False


async def _locked(db: AsyncSession, withdrawal_id: uuid.UUID) -> Withdrawal:
    w = (await db.execute(select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update()
                          .execution_options(populate_existing=True))).scalar_one_or_none()
    if w is None:
        raise FinanceError("withdrawal not found", 404)
    return w


async def approve(db: AsyncSession, *, withdrawal_id: uuid.UUID, admin, note: str | None = None,
                  now: datetime | None = None) -> Withdrawal:
    now = now or utcnow()
    w = await _locked(db, withdrawal_id)
    if w.status not in (WS.SECURITY_HOLD, WS.RISK_REVIEW):
        raise FinanceError(f"cannot approve a withdrawal in {w.status.value}")
    if await controls.is_paused(db, "PAUSE_WITHDRAWALS"):
        raise FinanceError("withdrawal approvals are paused (kill switch)")
    if w.eligible_at and now < w.eligible_at:
        raise FinanceError(f"security hold until {w.eligible_at.isoformat()}")
    if w.requested_by == admin.id and not settings.WITHDRAWAL_ALLOW_SELF_APPROVAL:
        raise FinanceError("requester cannot approve their own withdrawal", 403)
    approvals = list(w.approvals or [])
    if any(a.get("by") == str(admin.id) for a in approvals):
        raise FinanceError("this admin already approved")
    wallet = (await db.execute(select(Wallet).where(Wallet.id == w.wallet_id))).scalar_one()
    provider = (await db.execute(select(Provider).where(Provider.id == w.provider_id))).scalar_one()
    if not wallet.is_verified or wallet.address != w.recipient_address:
        raise FinanceError("payout wallet changed or lost verification since request")
    if provider.payout_hold:
        raise FinanceError("provider is on payout hold")
    approvals.append({"by": str(admin.id), "at": now.isoformat(), "note": note})
    w.approvals = approvals
    if len(approvals) >= int(w.required_approvals or 1):
        w.status = WS.APPROVED
        w.approved_at = now
        w.decided_by = admin.id
        w.approval_hash = approval_hash(withdrawal_id=str(w.id), provider_id=str(w.provider_id),
                                        recipient=w.recipient_address, amount_atomic=int(w.amount_atomic),
                                        asset=w.asset, chain_id=int(w.chain_id), approvers=[a["by"] for a in approvals])
    db.add(AuditLog(event_type=AuditEventType.WITHDRAWAL_APPROVED, actor_type="ADMIN", actor_id=admin.id,
                    target_type="WITHDRAWAL", target_id=w.id,
                    event_metadata={"approvals": len(approvals), "required": w.required_approvals,
                                    "final": w.status == WS.APPROVED, "note": note}))
    await db.flush()
    return w


async def reject(db: AsyncSession, *, withdrawal_id: uuid.UUID, admin, reason: str,
                 now: datetime | None = None) -> Withdrawal:
    w = await _locked(db, withdrawal_id)
    if w.status not in CANCELLABLE or w.payout_id is not None:
        raise FinanceError(f"cannot reject a withdrawal in {w.status.value}")
    await _release(db, w, f"rejected: {reason}", admin.id, "ADMIN")
    w.status, w.failure_reason, w.decided_by = WS.REJECTED, reason, admin.id
    db.add(AuditLog(event_type=AuditEventType.WITHDRAWAL_REJECTED, actor_type="ADMIN", actor_id=admin.id,
                    target_type="WITHDRAWAL", target_id=w.id, event_metadata={"reason": reason}))
    await db.flush()
    return w


async def cancel(db: AsyncSession, *, withdrawal_id: uuid.UUID, user, now: datetime | None = None) -> Withdrawal:
    w = await _locked(db, withdrawal_id)
    provider = (await db.execute(select(Provider).where(Provider.user_id == user.id))).scalars().first()
    if provider is None or provider.id != w.provider_id:
        raise FinanceError("withdrawal not found", 404)
    if w.status not in CANCELLABLE or w.payout_id is not None:
        raise FinanceError(f"cannot cancel a withdrawal in {w.status.value}")
    await _release(db, w, "cancelled by provider", user.id, "USER")
    w.status = WS.CANCELLED
    db.add(AuditLog(event_type=AuditEventType.WITHDRAWAL_CANCELLED, actor_type="USER", actor_id=user.id,
                    target_type="WITHDRAWAL", target_id=w.id, event_metadata={}))
    await db.flush()
    return w


def withdrawal_dict(w: Withdrawal, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    iso = lambda t: t.isoformat() if t else None  # noqa: E731
    return {"withdrawal_id": str(w.id), "provider_id": str(w.provider_id), "wallet_id": str(w.wallet_id),
            "book": w.book, "asset": w.asset, "network": w.network, "chain_id": w.chain_id,
            "recipient": w.recipient_address, "amount": str(d(w.amount)), "amount_atomic": w.amount_atomic,
            "status": w.status.value, "risk_score": float(w.risk_score or 0), "risk_decision": w.risk_decision,
            "risk_reasons": w.risk_reasons or [], "approvals": w.approvals or [],
            "required_approvals": w.required_approvals, "eligible_at": iso(w.eligible_at),
            "hold_remaining_seconds": max(0, int((w.eligible_at - now).total_seconds())) if w.eligible_at else 0,
            "requested_at": iso(w.requested_at), "approved_at": iso(w.approved_at),
            "broadcast_at": iso(w.broadcast_at), "confirmed_at": iso(w.confirmed_at), "tx_hash": w.tx_hash,
            "payout_id": str(w.payout_id) if w.payout_id else None, "failure_reason": w.failure_reason,
            "simulated": True}
