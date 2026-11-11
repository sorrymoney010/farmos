"""Treasury reconciliation (spec §137), daily close (spec §138) and platform
owner accounting (spec §26: wallet balance != company profit)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.enums import AuditEventType, LedgerDirection, WithdrawalStatus as WS
from app.db.models.finance import DailyClose, LedgerTransaction, Payout, ReconciliationRun
from app.db.models.jobs_ledger import LedgerAccount, LedgerEntry, ProviderBalance, RevenueAdapterRecord, Withdrawal
from app.payouts import get_provider
from app.services.finance import controls, ledger
from app.services.finance.controls import FinanceError
from app.services.finance.ledger import ZERO, d
from app.services.finance.payouts import in_flight_amount
from app.services.finance.withdrawals import OPEN

TOL = Decimal(str(settings.RECONCILIATION_TOLERANCE_USD))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _provider_ledger(db: AsyncSession, book: str) -> dict[uuid.UUID, dict[str, Decimal]]:
    rows = (await db.execute(
        select(LedgerAccount.owner_id, LedgerAccount.code,
               func.coalesce(func.sum(case((LedgerEntry.direction == LedgerDirection.CREDIT, LedgerEntry.amount),
                                           else_=-LedgerEntry.amount)), 0))
        .join(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
        .where(LedgerAccount.book == book, LedgerAccount.code.in_(list(ledger.PROVIDER_FIELDS)),
               LedgerAccount.asset == ledger.book_asset(book))
        .group_by(LedgerAccount.owner_id, LedgerAccount.code))).all()
    out: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for oid, code, bal in rows:
        out[oid][ledger.PROVIDER_FIELDS[code]] = d(bal)
    return out


async def run(db: AsyncSession, *, book: str = ledger.TEST, actor_id: uuid.UUID | None = None,
              now: datetime | None = None) -> ReconciliationRun:
    now = now or utcnow()
    checks: dict[str, Any] = {}

    tb = await ledger.trial_balance(db, book)
    checks["ledger_balanced"] = {"ok": tb["balanced"], "totals": tb["totals"],
                                 "unbalanced_transactions": tb["unbalanced_transactions"]}

    # 1. ledger provider liabilities vs provider balance aggregates
    led = await _provider_ledger(db, book)
    proj = {r.provider_id: r for r in (await db.execute(
        select(ProviderBalance).where(ProviderBalance.book == book))).scalars().all()}
    mism = []
    for pid in set(led) | set(proj):
        row = proj.get(pid)
        for field in ("pending_balance", "available_balance", "held_balance"):
            lv = led.get(pid, {}).get(field, ZERO)
            pv = d(getattr(row, field)) if row else ZERO
            if abs(lv - pv) > TOL:
                mism.append({"provider_id": str(pid), "field": field, "ledger": str(lv), "projection": str(pv)})
    checks["provider_liabilities"] = {"ok": not mism, "mismatches": mism,
                                      "total_liability": str(sum((sum(v.values(), ZERO) for v in led.values()), ZERO))}

    # 2. treasury ledger vs (simulated) chain balance, net of in-flight payouts
    treasury_ledger = await ledger.balance(db, "1000", book)
    if book == ledger.TEST:
        chain = get_provider()
        onchain = await chain.balance(settings.PAYOUT_HOT_WALLET_ADDRESS, "USDC", db=db)
        inflight = await in_flight_amount(db, book)
        delta = d(onchain - (treasury_ledger - inflight))
        checks["treasury"] = {"ok": abs(delta) <= TOL, "ledger_1000": str(treasury_ledger), "chain": str(onchain),
                              "in_flight_payouts": str(inflight), "delta": str(delta), "simulated": True}
    else:
        checks["treasury"] = {"ok": treasury_ledger == ZERO, "ledger_1000": str(treasury_ledger),
                              "chain": None, "note": "no REAL treasury is connected in this build"}

    # 3. withdrawal records vs payout transactions (+ held liabilities vs open withdrawals)
    problems = []
    ws = (await db.execute(select(Withdrawal).where(Withdrawal.book == book))).scalars().all()
    payouts = {p.withdrawal_id: p for p in (await db.execute(select(Payout).where(Payout.book == book))).scalars().all()}
    held_open: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for w in ws:
        p = payouts.get(w.id)
        if w.status in OPEN:
            held_open[w.provider_id] += d(w.amount)
        if w.status == WS.CONFIRMED:
            if p is None or p.status != "CONFIRMED" or w.settle_transaction_id is None:
                problems.append({"withdrawal_id": str(w.id), "issue": "confirmed without confirmed payout/settlement"})
            elif p.amount_atomic != w.amount_atomic or p.recipient.lower() != (w.recipient_address or "").lower():
                problems.append({"withdrawal_id": str(w.id), "issue": "payout amount/recipient mismatch"})
        if w.status in (WS.BROADCAST, WS.CONFIRMING) and (p is None or p.status not in ("BROADCAST", "SEEN_ONCHAIN", "CONFIRMING")):
            problems.append({"withdrawal_id": str(w.id), "issue": "in flight without payout"})
    known = {w.id for w in ws}
    for wid, p in payouts.items():
        if wid not in known:
            problems.append({"payout_id": str(p.id), "issue": "payout without withdrawal"})
    for pid in set(held_open) | set(led):
        lv = led.get(pid, {}).get("held_balance", ZERO)
        if abs(lv - held_open.get(pid, ZERO)) > TOL:
            problems.append({"provider_id": str(pid), "issue": "held ledger != open withdrawals",
                             "ledger": str(lv), "open_withdrawals": str(held_open.get(pid, ZERO))})
    checks["withdrawals_vs_payouts"] = {"ok": not problems, "problems": problems}

    # 4. adapter claimed rewards vs (simulated) external balances (Phase 3 reconcile)
    adapters = (await db.execute(select(RevenueAdapterRecord).where(RevenueAdapterRecord.kind == "EXTERNAL"))).scalars().all()
    bad = [a.id for a in adapters if (a.last_reconcile or {}).get("status") == "MISMATCH"]
    checks["adapters"] = {"ok": not bad, "mismatched": bad,
                          "last": {a.id: (a.last_reconcile or {}).get("status") for a in adapters}}

    ok = all(c["ok"] for c in checks.values())
    actions = []
    for name, c in checks.items():
        if not c["ok"]:
            ev = await controls.raise_risk(db, "RECONCILIATION", f"{name} mismatch ({book} book)", severity="HIGH",
                                           details={"check": c})
            actions.append({"risk_event_id": str(ev.id), "check": name})
    if not (checks["treasury"]["ok"] and checks["withdrawals_vs_payouts"]["ok"]) and \
            not await controls.is_paused(db, "PAUSE_PAYOUTS"):
        await controls.set_switch(db, "PAUSE_PAYOUTS", True, f"auto: reconciliation mismatch ({book})", None, "SYSTEM")
        actions.append({"kill_switch": "PAUSE_PAYOUTS", "enabled": True})  # spec §124 step 1
    rr = ReconciliationRun(book=book, status="OK" if ok else "MISMATCH", checks=checks, actions=actions,
                           triggered_by=actor_id)
    db.add(rr)
    db.add(AuditLog(event_type=AuditEventType.RECONCILIATION_RUN, actor_type="ADMIN" if actor_id else "SYSTEM",
                    actor_id=actor_id, target_type="RECONCILIATION",
                    event_metadata={"book": book, "status": rr.status, "actions": actions}))
    await db.flush()
    return rr


def run_dict(rr: ReconciliationRun) -> dict[str, Any]:
    return {"run_id": str(rr.id), "book": rr.book, "status": rr.status, "checks": rr.checks, "actions": rr.actions,
            "created_at": rr.created_at.isoformat() if rr.created_at else None}


# ── daily close ──────────────────────────────────────────────────────────────

async def _sum(db: AsyncSession, book: str, start: datetime, end: datetime, codes: list[str], direction: str,
               tx_types: list[str] | None = None) -> Decimal:
    q = (select(func.coalesce(func.sum(LedgerEntry.amount), 0)).select_from(LedgerEntry)
         .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
         .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
         .where(LedgerEntry.book == book, LedgerAccount.code.in_(codes), LedgerEntry.posted_at >= start,
                LedgerEntry.posted_at < end, LedgerEntry.direction == LedgerDirection(direction),
                LedgerAccount.asset == ledger.book_asset(book)))
    if tx_types:
        q = q.where(LedgerTransaction.tx_type.in_(tx_types))
    return d((await db.execute(q)).scalar() or 0)


async def daily_close(db: AsyncSession, *, close_date: date, book: str = ledger.TEST,
                      actor_id: uuid.UUID | None = None, now: datetime | None = None) -> tuple[DailyClose, bool]:
    now = now or utcnow()
    existing = (await db.execute(select(DailyClose).where(DailyClose.close_date == close_date,
                                                          DailyClose.book == book))).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if close_date > now.date():
        raise FinanceError("cannot close a future date", 400)
    start = datetime.combine(close_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    rr = await run(db, book=book, actor_id=actor_id, now=now)
    treasury_delta = rr.checks.get("treasury", {}).get("delta")
    liab_end = ZERO
    for code in ("2000", "2010", "2040"):
        liab_end += await ledger.balance(db, code, book, before=end)
    gross = await _sum(db, book, start, end, ["1020", "1030"], "DEBIT", ["JOB_REVENUE_CONFIRMED"])
    gross_rev = await _sum(db, book, start, end, ["1020", "1030"], "CREDIT", ["REVERSAL"])
    report = {
        "date": close_date.isoformat(), "book": book, "intraday": close_date == now.date(),
        "opening_treasury": str(await ledger.balance(db, "1000", book, before=start)),
        "customer_funding": str(await _sum(db, book, start, end, ["1000"], "DEBIT",
                                           ["CUSTOMER_FUNDING", "CUSTOMER_JOB_CHARGE"])),
        "gross_workload_revenue": str(gross - gross_rev),
        "token_reward_valuation": str(await _sum(db, book, start, end, ["1030"], "DEBIT", ["JOB_REVENUE_CONFIRMED"])),
        "provider_earnings": str(await _sum(db, book, start, end, ["2010", "2000"], "CREDIT",
                                            ["JOB_REVENUE_CONFIRMED", "TEST_CREDIT"])),
        "provider_payouts": str(await _sum(db, book, start, end, ["2040"], "DEBIT", ["PROVIDER_WITHDRAWAL_SETTLED"])),
        "network_fees": str(await _sum(db, book, start, end, ["5020"], "DEBIT")),
        "payment_fees": str(await _sum(db, book, start, end, ["5030"], "DEBIT")),
        "infrastructure_allocation": str(await _sum(db, book, start, end, ["4045"], "CREDIT", ["JOB_REVENUE_CONFIRMED"])),
        "closing_treasury": str(await ledger.balance(db, "1000", book, before=end)),
        "provider_liabilities": str(liab_end),
        "reconciliation_status": rr.status,
        "reconciliation_delta": treasury_delta,
        "transactions": int((await db.execute(select(func.count()).select_from(LedgerTransaction).where(
            LedgerTransaction.book == book, LedgerTransaction.posted_at >= start,
            LedgerTransaction.posted_at < end))).scalar() or 0),
    }
    dc = DailyClose(close_date=close_date, book=book, report=report, reconciliation_status=rr.status,
                    reconciliation_run_id=rr.id, closed_by=actor_id)
    db.add(dc)
    db.add(AuditLog(event_type=AuditEventType.DAILY_CLOSE, actor_type="ADMIN" if actor_id else "SYSTEM",
                    actor_id=actor_id, target_type="DAILY_CLOSE",
                    event_metadata={"date": close_date.isoformat(), "book": book, "status": rr.status}))
    await db.flush()
    return dc, True


def close_dict(dc: DailyClose) -> dict[str, Any]:
    return {"close_id": str(dc.id), "date": dc.close_date.isoformat(), "book": dc.book, "report": dc.report,
            "reconciliation_status": dc.reconciliation_status,
            "reconciliation_run_id": str(dc.reconciliation_run_id) if dc.reconciliation_run_id else None,
            "created_at": dc.created_at.isoformat() if dc.created_at else None}


# ── owner accounting (spec §26) ──────────────────────────────────────────────

async def summary(db: AsyncSession, book: str = ledger.TEST) -> dict[str, Any]:
    assets = {c: await ledger.balance(db, c, book) for c in ("1000", "1010", "1020", "1030", "1040", "1050")}
    pending = await ledger.balance(db, "2010", book)
    available = await ledger.balance(db, "2000", book)
    held = await ledger.balance(db, "2040", book)
    prepaid = await ledger.balance(db, "2020", book)
    accrued = await ledger.balance(db, "2030", book)
    reserve = d(settings.OPERATING_RESERVE_TARGET_USD)
    total_assets = sum(assets.values(), ZERO)
    liabilities = pending + available + held
    revenue = {c: str(await ledger.balance(db, c, book)) for c in ("4000", "4010", "4030", "4045")}
    last = (await db.execute(select(ReconciliationRun).where(ReconciliationRun.book == book)
                             .order_by(ReconciliationRun.created_at.desc()).limit(1))).scalar_one_or_none()
    out = {"book": book, "asset": ledger.book_asset(book),
           "assets": {ledger.CHART[c].name: str(v) for c, v in assets.items()}, "total_assets": str(total_assets),
           "provider_liabilities": {"pending": str(pending), "available": str(available), "held": str(held),
                                    "total": str(liabilities)},
           "customer_prepaid": str(prepaid), "accrued_costs": str(accrued), "operating_reserve": str(reserve),
           "available_company_funds": str(total_assets - liabilities - prepaid - accrued - reserve),
           "revenue_to_date": revenue, "kill_switches": await controls.kill_switches(db),
           "last_reconciliation": run_dict(last) if last else None,
           "note": "Wallet balance != company profit. TEST book = simulated billing/test credits, never real money."}
    if book == ledger.TEST:
        out["simulated_hot_wallet"] = str(await get_provider().balance(settings.PAYOUT_HOT_WALLET_ADDRESS, "USDC", db=db))
    return out
