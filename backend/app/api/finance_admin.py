"""Admin finance: ledger, revenue confirmation, withdrawals approval queue, payouts,
reconciliation, daily close, treasury summary, kill switches and risk events.

Everything here operates on the TEST book / simulated chain in this build.
"""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.core import Provider
from app.db.models.enums import WithdrawalStatus
from app.db.models.finance import DailyClose, LedgerTransaction, Payout, ReconciliationRun, RiskEvent
from app.db.models.jobs_ledger import JobEconomics, Withdrawal
from app.db.session import get_db
from app.dependencies import require_roles
from app.services.finance import controls, ledger, payouts, revenue, treasury
from app.services.finance import withdrawals as wsvc
from app.services.finance.controls import FinanceError
from app.services.finance.ledger import LedgerError

router = APIRouter()
FIN = ("OWNER", "ADMIN", "FINANCE")
READ = ("OWNER", "ADMIN", "FINANCE", "OPERATIONS")


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, FinanceError):
        return HTTPException(status_code=exc.status_code, detail=exc.detail)
    return HTTPException(status_code=409, detail=str(exc))


def _book(book: str) -> str:
    b = book.upper()
    if b not in ledger.BOOKS:
        raise HTTPException(status_code=400, detail="book must be TEST or REAL")
    return b


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class NoteBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class TestGrant(BaseModel):
    provider_id: UUID
    amount: Decimal = Field(gt=0, le=Decimal("100000"), max_digits=20, decimal_places=6)
    memo: str | None = Field(default=None, max_length=200)


class ConfirmBody(BaseModel):
    job_ids: list[UUID] | None = None
    limit: int = Field(default=200, ge=1, le=1000)


class SplitBody(BaseModel):
    default: dict[str, float]
    overrides: dict[str, dict[str, float]] = {}


class SwitchBody(BaseModel):
    name: str
    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


class CloseBody(BaseModel):
    date: Date
    book: str = "TEST"


# ── ledger ───────────────────────────────────────────────────────────────────

@router.get("/chart")
async def chart(_=Depends(require_roles(*READ))):
    return ledger.chart_dict()


@router.get("/trial-balance")
async def trial_balance(book: str = Query("TEST"), db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    return await ledger.trial_balance(db, _book(book))


@router.get("/transactions")
async def transactions(book: str = Query("TEST"), tx_type: str | None = None, limit: int = Query(100, ge=1, le=500),
                       db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    q = select(LedgerTransaction).where(LedgerTransaction.book == _book(book))
    if tx_type:
        q = q.where(LedgerTransaction.tx_type == tx_type.upper())
    rows = (await db.execute(q.order_by(LedgerTransaction.posted_at.desc()).limit(limit))).scalars().all()
    return [ledger.tx_dict(t) for t in rows]


@router.get("/transactions/{tx_id}")
async def transaction(tx_id: UUID, db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    t = (await db.execute(select(LedgerTransaction).where(LedgerTransaction.id == tx_id))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return ledger.tx_dict(t, await ledger.entries_of(db, t.id))


@router.post("/transactions/{tx_id}/reverse")
async def reverse(tx_id: UUID, body: ReasonBody, db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    """Corrections are reversals, never edits (spec §23)."""
    try:
        orig = (await db.execute(select(LedgerTransaction).where(LedgerTransaction.id == tx_id))).scalar_one_or_none()
        if orig is not None and orig.tx_type.startswith("PROVIDER_WITHDRAWAL"):
            raise FinanceError("withdrawal postings are managed by the withdrawal lifecycle (reject/cancel)", 409)
        if orig is not None and any(a.code == "1000" for _, a in await ledger.entries_of(db, orig.id)):
            raise FinanceError("treasury cash postings mirror chain events; post a compensating transaction "
                               "(e.g. REFUND) instead of a reversal", 409)
        rev = await ledger.reverse(db, tx_id, body.reason, user.id)
        if orig is not None and orig.tx_type == "JOB_REVENUE_CONFIRMED":
            econ = (await db.execute(select(JobEconomics).where(
                JobEconomics.revenue_transaction_id == orig.id))).scalar_one_or_none()
            if econ is not None:
                econ.revenue_state = "REVERSED"
    except (FinanceError, LedgerError) as exc:
        await db.rollback()
        raise _http(exc)
    await db.commit()
    return ledger.tx_dict(rev, await ledger.entries_of(db, rev.id))


@router.post("/test-grant")
async def test_grant(body: TestGrant, db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN)),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128)):
    """TEST book only: credit a provider's available balance with test credits (never real money)."""
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if (await db.execute(select(Provider.id).where(Provider.id == body.provider_id))).first() is None:
        raise HTTPException(status_code=404, detail="provider not found")
    from app.config import settings  # noqa: PLC0415
    from app.payouts import get_provider  # noqa: PLC0415

    try:
        # Simulated testnet faucet: test USDC lands in the simulated hot wallet (Dr 1000) and is
        # owed to the provider (Cr 2000), so payouts and reconciliation work end to end.
        tx, created = await ledger.post(
            db, tx_type="TEST_CREDIT", book=ledger.TEST,
            lines=[ledger.dr("1000", body.amount), ledger.cr("2000", body.amount, body.provider_id)],
            idempotency_key=f"test-grant:{idempotency_key}", reference_type="TEST_GRANT",
            reference_id=body.provider_id, description=body.memo or "admin test credit grant (simulated faucet)",
            actor_type="ADMIN", actor_id=user.id, metadata={"simulated": True})
        if created:
            await get_provider().deposit(db, settings.PAYOUT_HOT_WALLET_ADDRESS, "USDC", body.amount,
                                         reference=f"test-grant:{tx.id}")
    except LedgerError as exc:
        await db.rollback()
        raise _http(exc)
    await db.commit()
    return {**ledger.tx_dict(tx), "created": created}


# ── revenue ──────────────────────────────────────────────────────────────────

@router.post("/revenue/confirm")
async def revenue_confirm(body: ConfirmBody, db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    try:
        report = await revenue.confirm_revenue(db, job_ids=body.job_ids, limit=body.limit, actor_id=user.id)
    except (FinanceError, LedgerError) as exc:
        await db.rollback()
        raise _http(exc)
    await db.commit()
    return report


@router.post("/revenue/release-pending")
async def revenue_release(db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    report = await revenue.release_pending(db, actor_id=user.id)
    await db.commit()
    return report


@router.get("/revenue/split-policy")
async def get_split(db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    return await revenue.split_policy(db)


@router.put("/revenue/split-policy")
async def put_split(body: SplitBody, db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    try:
        value = await revenue.set_split_policy(db, body.model_dump(), user.id)
    except (ValueError, FinanceError) as exc:
        raise HTTPException(status_code=400, detail=str(getattr(exc, "detail", exc)))
    await db.commit()
    return value


# ── withdrawals ──────────────────────────────────────────────────────────────

@router.get("/withdrawals")
async def list_withdrawals(status: str | None = None, limit: int = Query(100, ge=1, le=500),
                           db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    q = select(Withdrawal)
    if status:
        try:
            q = q.where(Withdrawal.status == WithdrawalStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown status")
    rows = (await db.execute(q.order_by(Withdrawal.requested_at.desc()).limit(limit))).scalars().all()
    return [wsvc.withdrawal_dict(w) for w in rows]


@router.post("/withdrawals/{withdrawal_id}/approve")
async def approve(withdrawal_id: UUID, body: NoteBody, db: AsyncSession = Depends(get_db),
                  user=Depends(require_roles(*FIN))):
    try:
        w = await wsvc.approve(db, withdrawal_id=withdrawal_id, admin=user, note=body.note)
    except FinanceError as exc:
        await db.rollback()
        raise _http(exc)
    await db.commit()
    return wsvc.withdrawal_dict(w)


@router.post("/withdrawals/{withdrawal_id}/reject")
async def reject(withdrawal_id: UUID, body: ReasonBody, db: AsyncSession = Depends(get_db),
                 user=Depends(require_roles(*FIN))):
    try:
        w = await wsvc.reject(db, withdrawal_id=withdrawal_id, admin=user, reason=body.reason)
    except FinanceError as exc:
        await db.rollback()
        raise _http(exc)
    await db.commit()
    return wsvc.withdrawal_dict(w)


# ── payouts ──────────────────────────────────────────────────────────────────

@router.post("/payouts/run-batch")
async def run_batch(db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    try:
        return await payouts.run_batch(db, actor_id=user.id)
    except FinanceError as exc:
        await db.rollback()
        raise _http(exc)


@router.post("/payouts/poll")
async def poll(db: AsyncSession = Depends(get_db), _=Depends(require_roles(*FIN))):
    return await payouts.poll(db)


@router.get("/payouts")
async def list_payouts(limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                       _=Depends(require_roles(*READ))):
    rows = (await db.execute(select(Payout).order_by(Payout.created_at.desc()).limit(limit))).scalars().all()
    return [payouts.payout_dict(p) for p in rows]


# ── reconciliation / close / treasury ────────────────────────────────────────

@router.post("/reconcile")
async def reconcile(book: str = Query("TEST"), db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    rr = await treasury.run(db, book=_book(book), actor_id=user.id)
    await db.commit()
    return treasury.run_dict(rr)


@router.get("/reconciliation-runs")
async def reconciliation_runs(limit: int = Query(20, ge=1, le=200), db: AsyncSession = Depends(get_db),
                              _=Depends(require_roles(*READ))):
    rows = (await db.execute(select(ReconciliationRun).order_by(ReconciliationRun.created_at.desc())
                             .limit(limit))).scalars().all()
    return [treasury.run_dict(r) for r in rows]


@router.post("/daily-close")
async def daily_close(body: CloseBody, db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    try:
        dc, created = await treasury.daily_close(db, close_date=body.date, book=_book(body.book), actor_id=user.id)
    except FinanceError as exc:
        await db.rollback()
        raise _http(exc)
    await db.commit()
    return {**treasury.close_dict(dc), "created": created}


@router.get("/daily-closes")
async def daily_closes(limit: int = Query(31, ge=1, le=366), db: AsyncSession = Depends(get_db),
                       _=Depends(require_roles(*READ))):
    rows = (await db.execute(select(DailyClose).order_by(DailyClose.close_date.desc()).limit(limit))).scalars().all()
    return [treasury.close_dict(r) for r in rows]


@router.get("/treasury/summary")
async def treasury_summary(book: str = Query("TEST"), db: AsyncSession = Depends(get_db),
                           _=Depends(require_roles(*READ))):
    return await treasury.summary(db, _book(book))


# ── kill switches / risk ─────────────────────────────────────────────────────

@router.get("/kill-switches")
async def get_switches(db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    return await controls.kill_switches(db)


@router.post("/kill-switches")
async def set_switch(body: SwitchBody, db: AsyncSession = Depends(get_db), user=Depends(require_roles(*FIN))):
    try:
        value = await controls.set_switch(db, body.name.upper(), body.enabled, body.reason, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {body.name.upper(): value}


@router.get("/risk-events")
async def risk_events(status: str | None = Query("OPEN"), limit: int = Query(100, ge=1, le=500),
                      db: AsyncSession = Depends(get_db), _=Depends(require_roles(*READ))):
    q = select(RiskEvent)
    if status:
        q = q.where(RiskEvent.status == status.upper())
    rows = (await db.execute(q.order_by(RiskEvent.created_at.desc()).limit(limit))).scalars().all()
    return [controls.risk_dict(r) for r in rows]


@router.post("/risk-events/{event_id}/resolve")
async def resolve(event_id: UUID, body: ReasonBody, db: AsyncSession = Depends(get_db),
                  user=Depends(require_roles(*FIN))) -> dict[str, Any]:
    ev = await controls.resolve_risk(db, event_id, body.reason, user.id)
    if ev is None:
        raise HTTPException(status_code=404, detail="risk event not found")
    await db.commit()
    return controls.risk_dict(ev)
