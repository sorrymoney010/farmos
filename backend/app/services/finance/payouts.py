"""Payout batching, signing and confirmation tracking (spec §106-§111).

Approved withdrawals -> batch -> isolated signer (mock, key-less) -> simulated
provider broadcast -> BROADCAST -> SEEN_ONCHAIN -> CONFIRMING -> CONFIRMED ->
ledger settlement (Dr 2040 Holds / Cr 1000 Treasury). A withdrawal is never
marked complete at broadcast time. Failures release the held funds.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.core import Wallet
from app.db.models.enums import AuditEventType, WithdrawalStatus as WS
from app.db.models.finance import Payout
from app.db.models.jobs_ledger import Withdrawal
from app.logging import get_logger
from app.payouts import get_provider, get_signer
from app.payouts.base import PayoutInstruction, ProviderRejected, SignerAttestation, SignerRejected
from app.services.finance import controls, ledger
from app.services.finance.controls import FinanceError
from app.services.finance.ledger import d
from app.services.finance.withdrawals import _release, to_atomic, token_for

logger = get_logger(__name__)
IN_FLIGHT = ("BROADCAST", "SEEN_ONCHAIN", "CONFIRMING")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _daily_total_atomic(db: AsyncSession, now: datetime) -> int:
    return int((await db.execute(select(func.coalesce(func.sum(Payout.amount_atomic), 0)).where(
        Payout.created_at >= now - timedelta(hours=24), Payout.status.notin_(("FAILED", "REJECTED"))))).scalar() or 0)


async def _fail(db: AsyncSession, w: Withdrawal, reason: str) -> None:
    await _release(db, w, f"payout failed: {reason}", None, "SYSTEM")
    w.status, w.failure_reason = WS.FAILED, reason[:2000]
    db.add(AuditLog(event_type=AuditEventType.PAYOUT_FAILED, actor_type="SYSTEM", target_type="WITHDRAWAL",
                    target_id=w.id, event_metadata={"reason": reason}))


async def run_batch(db: AsyncSession, *, now: datetime | None = None, actor_id: uuid.UUID | None = None) -> dict[str, Any]:
    now = now or utcnow()
    if await controls.is_paused(db, "PAUSE_PAYOUTS"):
        raise FinanceError("payouts are paused (emergency stop)")
    signer, chain = get_signer(), get_provider()
    ids = [r[0] for r in (await db.execute(
        select(Withdrawal.id).where(Withdrawal.status == WS.APPROVED, Withdrawal.payout_id.is_(None),
                                    Withdrawal.book == ledger.TEST)
        .order_by(Withdrawal.approved_at).limit(settings.PAYOUT_BATCH_SIZE))).all()]
    batch_id = uuid.uuid4()
    report: dict[str, Any] = {"batch_id": str(batch_id), "broadcast": [], "waiting": {}, "failed": {},
                              "simulated": True, "provider": chain.name, "signer": signer.name}
    for wid in ids:
        w = (await db.execute(select(Withdrawal).where(Withdrawal.id == wid).with_for_update(skip_locked=True)
                              .execution_options(populate_existing=True))).scalar_one_or_none()
        if w is None or w.status != WS.APPROVED or w.payout_id is not None:
            continue
        wallet = (await db.execute(select(Wallet).where(Wallet.id == w.wallet_id))).scalar_one()
        token = token_for(w.network) or {}
        payout_id = uuid.uuid4()
        ins = PayoutInstruction(
            payout_id=str(payout_id), withdrawal_id=str(w.id), provider_id=str(w.provider_id), asset=w.asset,
            network=w.network, chain_id=int(w.chain_id), token_contract=token.get("contract"),
            recipient=w.recipient_address, amount_atomic=int(w.amount_atomic), created_at=now.isoformat(),
            approval_hash=w.approval_hash or "", risk_status="APPROVED")
        att = SignerAttestation(
            withdrawal_status=w.status.value, wallet_verified=wallet.is_verified, wallet_address=wallet.address,
            approved_amount_atomic=to_atomic(d(w.amount), token.get("decimals", 6)),
            approvers=tuple(a["by"] for a in (w.approvals or [])),
            daily_total_atomic_before=await _daily_total_atomic(db, now),
            emergency_stop=await controls.is_paused(db, "PAUSE_PAYOUTS"),
            token_status=token.get("status"), token_decimals=token.get("decimals"),
            allowed_networks=dict(settings.PAYOUT_NETWORKS),
            daily_limit_atomic=to_atomic(Decimal(str(settings.PAYOUT_DAILY_LIMIT_USD))),
            max_tx_atomic=to_atomic(Decimal(str(settings.PAYOUT_MAX_TX_USD))))
        try:
            signed = signer.sign(ins, att)
        except SignerRejected as exc:
            if exc.retryable:
                w.failure_reason = f"signer: {exc}"
                report["waiting"][str(w.id)] = str(exc)
            else:
                await _fail(db, w, f"signer rejected: {exc}")
                report["failed"][str(w.id)] = str(exc)
            await db.commit()
            continue
        try:
            async with db.begin_nested():
                tx_hash = await chain.broadcast(signed, db=db)
        except ProviderRejected as exc:
            if exc.retryable:
                w.failure_reason = f"provider: {exc}"
                report["waiting"][str(w.id)] = str(exc)
            else:
                await _fail(db, w, f"provider rejected: {exc}")
                report["failed"][str(w.id)] = str(exc)
            await db.commit()
            continue
        db.add(Payout(id=payout_id, withdrawal_id=w.id, provider_id=w.provider_id, batch_id=batch_id, book=w.book,
                      asset=w.asset, network=w.network, chain_id=int(w.chain_id), token_contract=ins.token_contract,
                      recipient=ins.recipient, amount_atomic=ins.amount_atomic, approval_hash=ins.approval_hash,
                      signer=signed.signer, signature=signed.signature, provider_name=chain.name,
                      simulated=chain.simulated, status="BROADCAST", tx_hash=tx_hash, broadcast_at=now))
        w.status, w.broadcast_at, w.tx_hash, w.payout_id, w.failure_reason = WS.BROADCAST, now, tx_hash, payout_id, None
        db.add(AuditLog(event_type=AuditEventType.PAYOUT_BROADCAST, actor_type="ADMIN" if actor_id else "SYSTEM",
                        actor_id=actor_id, target_type="WITHDRAWAL", target_id=w.id,
                        event_metadata={"payout_id": str(payout_id), "tx_hash": tx_hash, "simulated": True,
                                        "amount_atomic": ins.amount_atomic, "recipient": ins.recipient}))
        await db.commit()
        report["broadcast"].append({"withdrawal_id": str(w.id), "payout_id": str(payout_id), "tx_hash": tx_hash})
    return report


async def poll(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """Advance in-flight payouts through finality states (spec §111)."""
    now = now or utcnow()
    chain = get_provider()
    ids = [r[0] for r in (await db.execute(select(Payout.id).where(Payout.status.in_(IN_FLIGHT))
                                           .order_by(Payout.broadcast_at))).all()]
    report: dict[str, Any] = {"confirmed": [], "failed": [], "pending": []}
    for pid in ids:
        p = (await db.execute(select(Payout).where(Payout.id == pid).with_for_update(skip_locked=True)
                              .execution_options(populate_existing=True))).scalar_one_or_none()
        if p is None or p.status not in IN_FLIGHT:
            continue
        w = (await db.execute(select(Withdrawal).where(Withdrawal.id == p.withdrawal_id).with_for_update()
                              .execution_options(populate_existing=True))).scalar_one()
        st = await chain.status(p.tx_hash, db=db)
        p.confirmations, p.block_number = st.confirmations, st.block_number
        if st.state == "CONFIRMED":
            amt = d(w.amount)
            tx, _ = await ledger.post(db, tx_type="PROVIDER_WITHDRAWAL_SETTLED", book=w.book,
                                      lines=[ledger.dr("2040", amt, w.provider_id), ledger.cr("1000", amt)],
                                      idempotency_key=f"withdrawal-settle:{w.id}", reference_type="WITHDRAWAL",
                                      reference_id=w.id, description=f"payout confirmed {p.tx_hash}",
                                      metadata={"tx_hash": p.tx_hash, "payout_id": str(p.id), "simulated": p.simulated})
            p.status, p.confirmed_at = "CONFIRMED", now
            w.status, w.confirmed_at, w.settle_transaction_id = WS.CONFIRMED, now, tx.id
            db.add(AuditLog(event_type=AuditEventType.PAYOUT_CONFIRMED, actor_type="SYSTEM", target_type="WITHDRAWAL",
                            target_id=w.id, event_metadata={"tx_hash": p.tx_hash, "block": p.block_number,
                                                            "confirmations": p.confirmations, "simulated": p.simulated}))
            report["confirmed"].append(str(w.id))
        elif st.state == "FAILED":
            p.status, p.failure_reason = "FAILED", st.detail or "failed on chain"
            await _fail(db, w, p.failure_reason)
            report["failed"].append(str(w.id))
        else:
            if st.state in ("SEEN_ONCHAIN", "CONFIRMING"):
                p.status = st.state
                w.status = WS.CONFIRMING
            report["pending"].append(str(w.id))
        await db.commit()
    return report


async def in_flight_amount(db: AsyncSession, book: str) -> Decimal:
    """Payout value already moved on the (simulated) chain but not yet settled in the ledger."""
    rows = (await db.execute(select(Payout.amount_atomic, Payout.asset, Payout.network).where(
        Payout.book == book, Payout.status.in_(IN_FLIGHT)))).all()
    total = Decimal(0)
    for atomic, asset, network in rows:
        dec = settings.TOKEN_REGISTRY.get(asset, {}).get(network, {}).get("decimals", 6)
        total += Decimal(int(atomic)).scaleb(-dec)
    return d(total)


def payout_dict(p: Payout) -> dict[str, Any]:
    iso = lambda t: t.isoformat() if t else None  # noqa: E731
    return {"payout_id": str(p.id), "withdrawal_id": str(p.withdrawal_id), "provider_id": str(p.provider_id),
            "batch_id": str(p.batch_id) if p.batch_id else None, "book": p.book, "asset": p.asset,
            "network": p.network, "chain_id": p.chain_id, "recipient": p.recipient, "amount_atomic": p.amount_atomic,
            "status": p.status, "tx_hash": p.tx_hash, "block_number": p.block_number,
            "confirmations": p.confirmations, "signer": p.signer, "provider": p.provider_name,
            "simulated": p.simulated, "failure_reason": p.failure_reason, "created_at": iso(p.created_at),
            "broadcast_at": iso(p.broadcast_at), "confirmed_at": iso(p.confirmed_at)}
