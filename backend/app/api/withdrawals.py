"""Withdrawals endpoints for FARMOS."""

from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.jobs_ledger import Withdrawal
from app.db.models.enums import WithdrawalStatus
from app.db.session import get_db
from app.dependencies import require_roles, get_current_user
from app.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


class WithdrawalRequest(BaseModel):
    wallet_id: str
    asset: str
    network: str
    amount: float


@router.post("/")
async def request_withdrawal(
    req: WithdrawalRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "PROVIDER", "FINANCE")),
):
    w = Withdrawal(
        provider_id=current_user.id,
        wallet_id=req.wallet_id,
        asset=req.asset,
        network=req.network,
        amount=req.amount,
        status=WithdrawalStatus.REQUESTED,
    )
    db.add(w)
    await db.commit()
    logger.info("withdrawal_requested", withdrawal_id=str(w.id))
    return {
        "withdrawal_id": str(w.id),
        "status": WithdrawalStatus.REQUESTED.value,
        "eligible_at": w.requested_at.isoformat() if w.requested_at else None,
    }


@router.get("/")
async def list_withdrawals(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("OWNER", "ADMIN", "FINANCE", "PROVIDER")),
):
    result = await db.execute(select(Withdrawal).order_by(Withdrawal.requested_at.desc()).limit(100))
    withdrawals = result.scalars().all()
    return [
        {
            "withdrawal_id": str(w.id),
            "status": w.status.value,
            "asset": w.asset,
            "amount": float(w.amount),
            "requested_at": w.requested_at.isoformat() if w.requested_at else None,
        }
        for w in withdrawals
    ]
