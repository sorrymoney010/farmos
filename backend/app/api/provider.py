"""Provider endpoints: onboarding and dashboard views (spec §19 provider dashboard)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.core import Provider, Role, UserRole, Wallet
from app.db.models.devices import Device
from app.db.models.enums import AuditEventType, ProviderStatus, RoleName
from app.db.models.finance import LedgerTransaction
from app.db.models.jobs_ledger import Job, JobAssignment, JobEconomics, LedgerAccount, LedgerEntry, Withdrawal
from app.db.session import get_db
from app.dependencies import get_current_user, require_roles
from app.logging import get_logger
from app.services.finance import ledger
from app.services.finance.ledger import ZERO, d
from app.services.finance.wallets import wallet_dict
from app.services.finance.withdrawals import withdrawal_dict

logger = get_logger(__name__)
router = APIRouter()
ROLES = ("OWNER", "ADMIN", "PROVIDER", "OPERATIONS")


class OnboardRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)


async def _provider(db: AsyncSession, user) -> Provider | None:
    return (await db.execute(select(Provider).where(Provider.user_id == user.id))).scalars().first()


async def _balances(db: AsyncSession, provider: Provider) -> dict:
    from app.db.models.jobs_ledger import ProviderBalance  # noqa: PLC0415
    rows = {r.book: r for r in (await db.execute(select(ProviderBalance).where(
        ProviderBalance.provider_id == provider.id))).scalars().all()}
    out = {}
    for book in ledger.BOOKS:
        r = rows.get(book)
        f = (lambda name: str(d(getattr(r, name) or 0)) if r else str(d(0)))  # noqa: E731
        out[book] = {"asset": ledger.book_asset(book), "pending": f("pending_balance"),
                     "available": f("available_balance"), "held": f("held_balance"),
                     "lifetime_earned": f("lifetime_earned"), "lifetime_paid": f("lifetime_paid"),
                     "simulated": book == ledger.TEST}
    return out


@router.post("/onboard")
async def provider_onboard(req: OnboardRequest, db: AsyncSession = Depends(get_db),
                           current_user=Depends(get_current_user)):
    """Create the caller's provider account (ACTIVE) and grant the PROVIDER role."""
    provider = await _provider(db, current_user)
    created = provider is None
    if created:
        provider = Provider(user_id=current_user.id, display_name=req.display_name, status=ProviderStatus.ACTIVE)
        db.add(provider)
    role = (await db.execute(select(Role).where(Role.name == RoleName.PROVIDER))).scalar_one_or_none()
    if role is None:
        role = Role(name=RoleName.PROVIDER, description="Device provider")
        db.add(role)
        await db.flush()
    has = (await db.execute(select(UserRole).where(UserRole.user_id == current_user.id,
                                                   UserRole.role_id == role.id))).scalars().first()
    if has is None:
        db.add(UserRole(user_id=current_user.id, role_id=role.id))
    await db.flush()
    if created:
        db.add(AuditLog(event_type=AuditEventType.PROVIDER_CREATED, actor_type="USER", actor_id=current_user.id,
                        target_type="PROVIDER", target_id=provider.id, event_metadata={"action": "provider_onboard"}))
    await db.commit()
    return {"provider_id": str(provider.id), "display_name": provider.display_name, "status": provider.status.value,
            "created": created}


@router.get("/balances")
async def provider_balances(db: AsyncSession = Depends(get_db), current_user=Depends(require_roles(*ROLES))):
    provider = await _provider(db, current_user)
    if provider is None:
        raise HTTPException(status_code=404, detail="no provider account")
    return {"provider_id": str(provider.id), "payout_hold": provider.payout_hold,
            "balances": await _balances(db, provider)}


@router.get("/ledger")
async def provider_ledger(book: str = Query("TEST"), limit: int = Query(50, ge=1, le=200),
                          db: AsyncSession = Depends(get_db), current_user=Depends(require_roles(*ROLES))):
    """The provider's own liability-account movements (never other providers' entries)."""
    provider = await _provider(db, current_user)
    if provider is None:
        raise HTTPException(status_code=404, detail="no provider account")
    rows = (await db.execute(
        select(LedgerEntry, LedgerAccount, LedgerTransaction)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
        .where(LedgerAccount.owner_type == "PROVIDER", LedgerAccount.owner_id == provider.id,
               LedgerAccount.book == book.upper())
        .order_by(LedgerEntry.posted_at.desc()).limit(limit))).all()
    return [{"transaction_id": str(t.id), "tx_type": t.tx_type, "book": t.book, "account": a.name, "code": a.code,
             "direction": e.direction.value, "amount": str(d(e.amount)), "asset": e.asset,
             "description": t.description, "posted_at": e.posted_at.isoformat() if e.posted_at else None}
            for e, a, t in rows]


@router.get("/dashboard")
async def provider_dashboard(db: AsyncSession = Depends(get_db), current_user=Depends(require_roles(*ROLES))):
    """Devices, the provider's own jobs, balances (TEST vs REAL), wallets and withdrawals."""
    provider = await _provider(db, current_user)
    if not provider:
        return {"online_devices": 0, "devices": [], "jobs": {"pending": 0, "running": 0, "complete": 0, "recent": []},
                "earnings": {"total_credits": 0.0, "total_debits": 0.0, "net": 0.0}, "balances": {},
                "wallets": [], "withdrawals": []}

    devices = (await db.execute(select(Device).where(Device.provider_id == provider.id))).scalars().all()
    device_ids = [dv.id for dv in devices]
    jobs = []
    if device_ids:
        jobs = (await db.execute(select(Job).join(JobAssignment, JobAssignment.job_id == Job.id)
                                 .where(JobAssignment.device_id.in_(device_ids))
                                 .order_by(Job.created_at.desc()).limit(50))).scalars().all()
    pending_statuses = {"CREATED", "QUEUED", "DISPATCHED"}
    running_statuses = {"ACCEPTED", "RUNNING", "RESULT_SUBMITTED", "VERIFYING"}
    complete_statuses = {"COMPLETE", "COMPLETED", "VERIFIED", "PAID"}
    pending = running = complete = 0
    recent = []
    for j in jobs:
        st = j.status.value if hasattr(j.status, "value") else str(j.status)
        pending += st in pending_statuses
        running += st in running_statuses
        complete += st in complete_statuses
        if len(recent) < 20:
            recent.append({"job_id": str(j.id), "status": st, "workload_type": j.workload_type,
                           "created_at": j.created_at.isoformat() if j.created_at else None})

    balances = await _balances(db, provider)
    test = balances[ledger.TEST]
    unconfirmed = [] if not device_ids else (await db.execute(select(JobEconomics).where(
        JobEconomics.device_id.in_(device_ids), JobEconomics.revenue_state.in_(("UNCONFIRMED", "SIMULATED"))))).scalars().all()
    estimated = sum((d(e.actual_gross_usd or 0) for e in unconfirmed), ZERO) if unconfirmed else ZERO
    wallets = (await db.execute(select(Wallet).where(Wallet.provider_id == provider.id))).scalars().all()
    wds = (await db.execute(select(Withdrawal).where(Withdrawal.provider_id == provider.id)
                            .order_by(Withdrawal.requested_at.desc()).limit(20))).scalars().all()
    now = datetime.now(timezone.utc)
    return {
        "provider_id": str(provider.id), "display_name": provider.display_name, "status": provider.status.value,
        "payout_hold": provider.payout_hold,
        "online_devices": len([dv for dv in devices if dv.status.value in ("ACTIVE", "RUNNING", "IDLE")]),
        "devices": [{"device_id": str(dv.id), "human_id": dv.human_id, "status": dv.status.value,
                     "farm_score": float(dv.farm_score) if dv.farm_score is not None else None,
                     "last_seen_at": dv.last_seen_at.isoformat() if dv.last_seen_at else None} for dv in devices],
        "jobs": {"pending": pending, "running": running, "complete": complete, "recent": recent},
        # legacy keys kept for older UIs; now provider-scoped (TEST book) instead of global ledger sums
        "earnings": {"total_credits": float(test["lifetime_earned"]), "total_debits": float(test["lifetime_paid"]),
                     "net": float(d(test["pending"]) + d(test["available"]) + d(test["held"])),
                     "estimated_unconfirmed": str(estimated), "book": ledger.TEST},
        "balances": balances,
        "wallets": [wallet_dict(w, now) for w in wallets],
        "withdrawals": [withdrawal_dict(w, now) for w in wds],
    }
