"""Double-entry ledger (spec §23, §66, §98-§100; coder rules §53.6/§53.7).

* Every posting is a typed ``LedgerTransaction`` with >= 2 lines whose debits
  equal credits per asset. Unknown transaction types and accounts a type may
  not touch are rejected (spec §100: no ad-hoc types).
* Books: ``TEST`` (simulated billing / test credits, asset TEST_USD) and
  ``REAL`` (USDC). A transaction never mixes books.
* Idempotent: one transaction per ``idempotency_key``; replays return it.
* Append-only (DB trigger); corrections are REVERSAL transactions.
* Provider balance projection (pending/available/held/lifetime) is updated in
  the same DB transaction and reconciled against the ledger (spec §137).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.enums import AuditEventType, LedgerAccountType, LedgerDirection
from app.db.models.finance import LedgerTransaction
from app.db.models.jobs_ledger import LedgerAccount, LedgerEntry, ProviderBalance

Q = Decimal("0.000000000001")
ZERO = Decimal(0)
TEST, REAL = "TEST", "REAL"
BOOKS = (TEST, REAL)
PLATFORM_ID = uuid.UUID(int=0)
DR, CR = "DEBIT", "CREDIT"


class LedgerError(ValueError):
    pass


def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x.quantize(Q, rounding=ROUND_HALF_EVEN)
    return Decimal(str(x)).quantize(Q, rounding=ROUND_HALF_EVEN)


def book_asset(book: str) -> str:
    return settings.LEDGER_TEST_ASSET if book == TEST else settings.LEDGER_REAL_ASSET


@dataclass(frozen=True)
class AccountSpec:
    code: str
    name: str
    category: str      # ASSET|LIABILITY|REVENUE|EXPENSE|RESERVE|CONTRA
    scope: str = "PLATFORM"  # PLATFORM|PROVIDER|CUSTOMER|DEVICE
    test_only: bool = False

    @property
    def normal_side(self) -> str:
        return DR if self.category in ("ASSET", "EXPENSE", "CONTRA") else CR


# Spec §99 V1.2 chart of accounts (+ 4045 from the §66 posting example, + TEST-book accounts).
CHART: dict[str, AccountSpec] = {s.code: s for s in [
    AccountSpec("1000", "Treasury - USDC Base", "ASSET"),
    AccountSpec("1010", "Treasury - ETH Base", "ASSET"),
    AccountSpec("1020", "Revenue Clearing - Fiat", "ASSET"),
    AccountSpec("1030", "Revenue Clearing - Crypto", "ASSET"),
    AccountSpec("1040", "Exchange / Conversion Clearing", "ASSET"),
    AccountSpec("1050", "Customer Funds Clearing", "ASSET"),
    AccountSpec("2000", "Provider Payable", "LIABILITY", "PROVIDER"),
    AccountSpec("2010", "Provider Pending Earnings", "LIABILITY", "PROVIDER"),
    AccountSpec("2020", "Customer Prepaid Liability", "LIABILITY", "CUSTOMER"),
    AccountSpec("2030", "Network Fees Payable", "LIABILITY"),
    AccountSpec("2040", "Withdrawal Holds", "LIABILITY", "PROVIDER"),
    AccountSpec("4000", "Compute Revenue", "REVENUE"),
    AccountSpec("4010", "Monitoring Revenue", "REVENUE"),
    AccountSpec("4020", "Storage Revenue", "REVENUE"),
    AccountSpec("4030", "DePIN Revenue", "REVENUE"),
    AccountSpec("4040", "Platform Fees", "REVENUE"),
    AccountSpec("4045", "Infrastructure Recovery", "REVENUE"),
    AccountSpec("4050", "Other Approved Revenue", "REVENUE"),
    AccountSpec("5000", "Provider Revenue Share", "EXPENSE"),
    AccountSpec("5010", "Cloud Infrastructure", "EXPENSE"),
    AccountSpec("5020", "Blockchain Fees", "EXPENSE"),
    AccountSpec("5030", "Payment Processing", "EXPENSE"),
    AccountSpec("5040", "Device Energy Allocation", "EXPENSE"),
    AccountSpec("5050", "Device Depreciation Allocation", "EXPENSE"),
    AccountSpec("5060", "External API Costs", "EXPENSE"),
    AccountSpec("5070", "Fraud / Chargeback Loss", "EXPENSE"),
    AccountSpec("6000", "Operating Reserve Allocation", "RESERVE"),
    AccountSpec("6010", "Hardware Reinvestment Allocation", "RESERVE"),
    AccountSpec("9000", "Test Credit Issuance (contra)", "CONTRA", test_only=True),
    AccountSpec("9010", "Device Test Earnings (legacy)", "LIABILITY", "DEVICE", test_only=True),
]}

PROVIDER_FIELDS = {"2010": "pending_balance", "2000": "available_balance", "2040": "held_balance"}

# Spec §100 transaction types -> account codes each type may touch.
TX_RULES: dict[str, Optional[set[str]]] = {
    "CUSTOMER_FUNDING": {"1000", "1020", "1030", "1050", "2020"},
    "CUSTOMER_JOB_CHARGE": {"1000", "1020", "1050", "2020"},
    "JOB_REVENUE_CONFIRMED": {"1020", "1030", "2010", "4000", "4010", "4020", "4030", "4045", "4050"},
    "PROVIDER_EARNING_PENDING": {"1020", "1030", "2010"},
    "PROVIDER_EARNING_AVAILABLE": {"2010", "2000"},
    "PROVIDER_WITHDRAWAL_HOLD": {"2000", "2040"},
    "PROVIDER_WITHDRAWAL_SETTLED": {"2040", "1000"},
    "WITHDRAWAL_RELEASE": {"2040", "2000"},
    "CHAIN_FEE": {"5020", "1000", "1010", "2030"},
    "TOKEN_CONVERSION": {"1030", "1040", "1000"},
    "TREASURY_TRANSFER": {"1000", "1010", "1040"},
    "REFUND": {"2020", "1000", "1050"},
    "REVERSAL": None,  # mirrors the original transaction's accounts
    "CHARGEBACK": {"5070", "1000", "1020", "2010", "2000"},
    "RESERVE_ALLOCATION": {"6000", "6010", "4000", "4010", "4030", "4040", "4045", "4050"},
    "TEST_CREDIT": {"9000", "9010", "1000", "2000", "2010"},  # 1000: simulated testnet faucet funding
}
TEST_ONLY_TYPES = {"TEST_CREDIT"}
EARNING_TYPES = {"JOB_REVENUE_CONFIRMED", "TEST_CREDIT"}


@dataclass
class Line:
    code: str
    direction: str
    amount: Any
    owner_id: Optional[uuid.UUID] = None
    asset: Optional[str] = None
    memo: Optional[str] = None


def dr(code: str, amount, owner_id=None, asset=None, memo=None) -> Line:
    return Line(code, DR, amount, owner_id, asset, memo)


def cr(code: str, amount, owner_id=None, asset=None, memo=None) -> Line:
    return Line(code, CR, amount, owner_id, asset, memo)


def validate_lines(tx_type: str, book: str, lines: list[Line]) -> list[Line]:
    """Pure validation (unit-tested): type, book, accounts, amounts, balance per asset."""
    if tx_type not in TX_RULES:
        raise LedgerError(f"unknown transaction type {tx_type}")
    if book not in BOOKS:
        raise LedgerError(f"unknown book {book}")
    if tx_type in TEST_ONLY_TYPES and book != TEST:
        raise LedgerError(f"{tx_type} is only allowed on the TEST book")
    if len(lines) < 2:
        raise LedgerError("a transaction needs at least two lines")
    allowed = TX_RULES[tx_type]
    out, sums = [], defaultdict(lambda: [ZERO, ZERO])
    for ln in lines:
        spec = CHART.get(ln.code)
        if spec is None:
            raise LedgerError(f"unknown account code {ln.code}")
        if allowed is not None and ln.code not in allowed:
            raise LedgerError(f"{tx_type} may not post to account {ln.code}")
        if spec.test_only and book != TEST:
            raise LedgerError(f"account {ln.code} exists only on the TEST book")
        if spec.scope != "PLATFORM" and ln.owner_id is None:
            raise LedgerError(f"account {ln.code} needs an owner ({spec.scope})")
        if ln.direction not in (DR, CR):
            raise LedgerError("direction must be DEBIT or CREDIT")
        amt = d(ln.amount)
        if amt <= ZERO:
            raise LedgerError("line amounts must be positive")
        asset = ln.asset or book_asset(book)
        if book == TEST and asset == settings.LEDGER_REAL_ASSET:
            raise LedgerError("TEST book cannot carry REAL USDC")
        if book == REAL and asset in (settings.LEDGER_TEST_ASSET, "USD"):
            raise LedgerError("REAL book cannot carry TEST credits")
        sums[asset][0 if ln.direction == DR else 1] += amt
        out.append(Line(ln.code, ln.direction, amt, ln.owner_id, asset, ln.memo))
    for asset, (dsum, csum) in sums.items():
        if dsum != csum:
            raise LedgerError(f"unbalanced {asset}: debits {dsum} != credits {csum}")
    return out


async def account(db: AsyncSession, code: str, book: str, owner_id: uuid.UUID | None = None,
                  asset: str | None = None) -> LedgerAccount:
    spec = CHART[code]
    owner_type = spec.scope
    oid = PLATFORM_ID if spec.scope == "PLATFORM" else owner_id
    if oid is None:
        raise LedgerError(f"account {code} needs an owner")
    asset = asset or book_asset(book)
    acct_type = LedgerAccountType.DEVICE_EARNINGS_TEST if code == "9010" else LedgerAccountType.CHART_ACCOUNT
    await db.execute(
        pg_insert(LedgerAccount).values(
            id=uuid.uuid4(), owner_type=owner_type, owner_id=oid, account_type=acct_type.name, asset=asset,
            code=code, name=spec.name, category=spec.category, normal_side=spec.normal_side, book=book,
        ).on_conflict_do_nothing(index_elements=["book", "code", "owner_type", "owner_id", "asset"],
                                 index_where=text("code IS NOT NULL"))
    )
    return (await db.execute(select(LedgerAccount).where(
        LedgerAccount.book == book, LedgerAccount.code == code, LedgerAccount.owner_type == owner_type,
        LedgerAccount.owner_id == oid, LedgerAccount.asset == asset))).scalar_one()


async def _project(db: AsyncSession, tx: LedgerTransaction, lines: list[Line], reverses_type: str | None) -> None:
    deltas: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for ln in lines:
        field = PROVIDER_FIELDS.get(ln.code)
        if field is None:
            continue
        signed = ln.amount if ln.direction == CR else -ln.amount
        dd = deltas[ln.owner_id]
        dd[field] += signed
        if tx.tx_type in EARNING_TYPES and ln.direction == CR and ln.code in ("2000", "2010"):
            dd["lifetime_earned"] += ln.amount
        if tx.tx_type == "REVERSAL" and reverses_type in EARNING_TYPES and ln.direction == DR and ln.code in ("2000", "2010"):
            dd["lifetime_earned"] -= ln.amount
        if tx.tx_type == "PROVIDER_WITHDRAWAL_SETTLED" and ln.code == "2040" and ln.direction == DR:
            dd["lifetime_paid"] += ln.amount
        if tx.tx_type == "REVERSAL" and reverses_type == "PROVIDER_WITHDRAWAL_SETTLED" and ln.code == "2040" and ln.direction == CR:
            dd["lifetime_paid"] -= ln.amount
    for provider_id, dd in deltas.items():
        pb = await provider_balance_row(db, provider_id, tx.book, lock=True)
        for field, delta in dd.items():
            setattr(pb, field, d(Decimal(str(getattr(pb, field) or 0)) + delta))
        for field in ("pending_balance", "available_balance", "held_balance"):
            if d(getattr(pb, field)) < ZERO:
                raise LedgerError(f"posting would make provider {field} negative")


async def provider_balance_row(db: AsyncSession, provider_id: uuid.UUID, book: str, *, lock: bool = False) -> ProviderBalance:
    await db.execute(pg_insert(ProviderBalance).values(
        id=uuid.uuid4(), provider_id=provider_id, book=book, asset=book_asset(book), pending_balance=0,
        available_balance=0, held_balance=0, lifetime_earned=0, lifetime_paid=0,
    ).on_conflict_do_nothing(constraint="uq_provider_balance_book"))
    q = select(ProviderBalance).where(ProviderBalance.provider_id == provider_id, ProviderBalance.book == book)
    if lock:
        q = q.with_for_update()
    return (await db.execute(q.execution_options(populate_existing=True))).scalar_one()


async def post(db: AsyncSession, *, tx_type: str, book: str, lines: list[Line], idempotency_key: str,
               reference_type: str | None = None, reference_id: uuid.UUID | None = None,
               description: str | None = None, actor_type: str = "SYSTEM", actor_id: uuid.UUID | None = None,
               metadata: dict | None = None, reverses: LedgerTransaction | None = None) -> tuple[LedgerTransaction, bool]:
    """Post a balanced transaction. Returns (transaction, created)."""
    existing = (await db.execute(select(LedgerTransaction).where(
        LedgerTransaction.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing is not None:
        if existing.tx_type != tx_type or existing.book != book:
            raise LedgerError("idempotency key reused for a different transaction")
        return existing, False
    clean = validate_lines(tx_type, book, lines)
    tx = LedgerTransaction(
        id=uuid.uuid4(), tx_type=tx_type, book=book, idempotency_key=idempotency_key,
        reference_type=reference_type, reference_id=reference_id, description=description,
        actor_type=actor_type, actor_id=actor_id, reverses_transaction_id=reverses.id if reverses else None,
        event_metadata=metadata or {},
    )
    try:
        async with db.begin_nested():  # all-or-nothing: a failed posting leaves no trace
            db.add(tx)
            await db.flush()
            for ln in clean:
                acct = await account(db, ln.code, book, ln.owner_id, ln.asset)
                db.add(LedgerEntry(
                    transaction_id=tx.id, account_id=acct.id, direction=LedgerDirection(ln.direction),
                    amount=ln.amount, asset=ln.asset, usd_value=ln.amount,
                    reference_type=reference_type or tx_type, reference_id=reference_id, book=book,
                    event_metadata={"tx_type": tx_type, "code": ln.code, **({"memo": ln.memo} if ln.memo else {})},
                ))
            await _project(db, tx, clean, (metadata or {}).get("reverses_type"))
            db.add(AuditLog(event_type=AuditEventType.LEDGER_POSTED, actor_type=actor_type, actor_id=actor_id,
                            target_type="LEDGER_TRANSACTION", target_id=tx.id,
                            event_metadata={"tx_type": tx_type, "book": book, "idempotency_key": idempotency_key,
                                            "total": str(sum((ln.amount for ln in clean if ln.direction == DR), ZERO))}))
            await db.flush()
    except IntegrityError as exc:
        existing = (await db.execute(select(LedgerTransaction).where(
            LedgerTransaction.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing is None:
            raise LedgerError(f"ledger integrity error: {exc.orig}") from exc
        return existing, False
    return tx, True


async def entries_of(db: AsyncSession, tx_id: uuid.UUID) -> list[tuple[LedgerEntry, LedgerAccount]]:
    return list((await db.execute(
        select(LedgerEntry, LedgerAccount).join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerEntry.transaction_id == tx_id).order_by(LedgerEntry.direction, LedgerAccount.code))).all())


async def reverse(db: AsyncSession, tx_id: uuid.UUID, reason: str, actor_id: uuid.UUID | None,
                  actor_type: str = "ADMIN") -> LedgerTransaction:
    orig = (await db.execute(select(LedgerTransaction).where(LedgerTransaction.id == tx_id))).scalar_one_or_none()
    if orig is None:
        raise LedgerError("transaction not found")
    if orig.tx_type == "REVERSAL":
        raise LedgerError("a reversal cannot be reversed; post a new transaction")
    lines = []
    for e, a in await entries_of(db, orig.id):
        if a.code is None:
            raise LedgerError("legacy entries without a chart code cannot be reversed")
        lines.append(Line(a.code, CR if e.direction == LedgerDirection.DEBIT else DR, e.amount,
                          None if a.owner_type == "PLATFORM" else a.owner_id, a.asset))
    tx, created = await post(db, tx_type="REVERSAL", book=orig.book, lines=lines, idempotency_key=f"reversal:{orig.id}",
                             reference_type=orig.reference_type, reference_id=orig.reference_id,
                             description=f"reversal of {orig.tx_type}: {reason}", actor_type=actor_type,
                             actor_id=actor_id, metadata={"reverses_type": orig.tx_type, "reason": reason}, reverses=orig)
    if created:
        db.add(AuditLog(event_type=AuditEventType.LEDGER_REVERSED, actor_type=actor_type, actor_id=actor_id,
                        target_type="LEDGER_TRANSACTION", target_id=orig.id,
                        event_metadata={"reversal_id": str(tx.id), "reason": reason}))
    return tx


# ── read models ──────────────────────────────────────────────────────────────

def _signed_sum(normal_side_col):
    return func.coalesce(func.sum(case(
        (LedgerEntry.direction == LedgerDirection.DEBIT, LedgerEntry.amount), else_=-LedgerEntry.amount)), 0)


async def balance(db: AsyncSession, code: str, book: str, owner_id: uuid.UUID | None = None,
                  asset: str | None = None, before=None) -> Decimal:
    """Normal-side balance of one account (or all owners of a code when owner_id is None)."""
    spec = CHART[code]
    q = (select(_signed_sum(None)).select_from(LedgerEntry).join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
         .where(LedgerAccount.code == code, LedgerAccount.book == book,
                LedgerAccount.asset == (asset or book_asset(book))))
    if spec.scope == "PLATFORM":
        q = q.where(LedgerAccount.owner_id == PLATFORM_ID)
    elif owner_id is not None:
        q = q.where(LedgerAccount.owner_id == owner_id)
    if before is not None:
        q = q.where(LedgerEntry.posted_at < before)
    net_debit = Decimal(str((await db.execute(q)).scalar() or 0))
    return d(net_debit if spec.normal_side == DR else -net_debit)


async def trial_balance(db: AsyncSession, book: str) -> dict[str, Any]:
    rows = (await db.execute(
        select(LedgerAccount.code, LedgerAccount.name, LedgerAccount.owner_type, LedgerAccount.owner_id,
               LedgerAccount.asset, LedgerAccount.normal_side,
               func.coalesce(func.sum(case((LedgerEntry.direction == LedgerDirection.DEBIT, LedgerEntry.amount), else_=0)), 0),
               func.coalesce(func.sum(case((LedgerEntry.direction == LedgerDirection.CREDIT, LedgerEntry.amount), else_=0)), 0))
        .join(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
        .where(LedgerAccount.book == book)
        .group_by(LedgerAccount.id).order_by(LedgerAccount.code))).all()
    accounts, totals = [], defaultdict(lambda: [ZERO, ZERO])
    for code, name, otype, oid, asset, side, dsum, csum in rows:
        dsum, csum = d(dsum), d(csum)
        totals[asset][0] += dsum
        totals[asset][1] += csum
        bal = dsum - csum if (side or DR) == DR else csum - dsum
        accounts.append({"code": code, "name": name, "owner_type": otype,
                         "owner_id": None if otype == "PLATFORM" or oid is None else str(oid), "asset": asset,
                         "debits": str(dsum), "credits": str(csum), "balance": str(d(bal))})
    unbalanced = await unbalanced_transactions(db, book)
    return {"book": book, "accounts": accounts,
            "totals": {a: {"debits": str(t[0]), "credits": str(t[1])} for a, t in totals.items()},
            "balanced": all(t[0] == t[1] for t in totals.values()) and not unbalanced,
            "unbalanced_transactions": unbalanced}


async def unbalanced_transactions(db: AsyncSession, book: str) -> list[str]:
    rows = (await db.execute(
        select(LedgerEntry.transaction_id, LedgerEntry.asset)
        .where(LedgerEntry.book == book)
        .group_by(LedgerEntry.transaction_id, LedgerEntry.asset)
        .having(func.sum(case((LedgerEntry.direction == LedgerDirection.DEBIT, LedgerEntry.amount),
                              else_=-LedgerEntry.amount)) != 0))).all()
    return sorted({str(r[0]) for r in rows})


def tx_dict(tx: LedgerTransaction, entries: Iterable[tuple[LedgerEntry, LedgerAccount]] | None = None) -> dict[str, Any]:
    out = {"transaction_id": str(tx.id), "tx_type": tx.tx_type, "book": tx.book,
           "idempotency_key": tx.idempotency_key, "reference_type": tx.reference_type,
           "reference_id": str(tx.reference_id) if tx.reference_id else None, "description": tx.description,
           "actor_type": tx.actor_type, "actor_id": str(tx.actor_id) if tx.actor_id else None,
           "reverses_transaction_id": str(tx.reverses_transaction_id) if tx.reverses_transaction_id else None,
           "metadata": tx.event_metadata or {}, "posted_at": tx.posted_at.isoformat() if tx.posted_at else None}
    if entries is not None:
        out["entries"] = [{"code": a.code, "account": a.name, "owner_type": a.owner_type,
                           "owner_id": None if a.owner_type == "PLATFORM" else str(a.owner_id),
                           "direction": e.direction.value, "amount": str(d(e.amount)), "asset": e.asset}
                          for e, a in entries]
    return out


def chart_dict() -> list[dict[str, Any]]:
    return [{"code": s.code, "name": s.name, "category": s.category, "normal_side": s.normal_side,
             "scope": s.scope, "test_only": s.test_only} for s in CHART.values()]
