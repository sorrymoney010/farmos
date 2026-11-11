"""Provider withdrawals (spec §24, §74, §106, §149). TEST book only in this build."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.core import Provider
from app.db.models.jobs_ledger import Withdrawal
from app.db.session import get_db
from app.dependencies import require_roles
from app.services.finance import withdrawals as wsvc
from app.services.finance.controls import FinanceError

router = APIRouter()
ROLES = ("OWNER", "ADMIN", "PROVIDER")


class WithdrawalRequest(BaseModel):
    wallet_id: UUID
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=6)
    book: str = "TEST"


@router.post("/")
async def request_withdrawal(req: WithdrawalRequest, db: AsyncSession = Depends(get_db),
                             current_user=Depends(require_roles(*ROLES)),
                             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128)):
    try:
        w, replay = await wsvc.request(db, user=current_user, wallet_id=req.wallet_id, amount=req.amount,
                                       book=req.book.upper(), idempotency_key=idempotency_key)
    except FinanceError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    await db.commit()
    return {**wsvc.withdrawal_dict(w), "idempotent_replay": replay}


@router.get("/")
async def list_withdrawals(db: AsyncSession = Depends(get_db), current_user=Depends(require_roles(*ROLES))):
    provider = (await db.execute(select(Provider).where(Provider.user_id == current_user.id))).scalars().first()
    if provider is None:
        return []
    rows = (await db.execute(select(Withdrawal).where(Withdrawal.provider_id == provider.id)
                             .order_by(Withdrawal.requested_at.desc()).limit(100))).scalars().all()
    return [wsvc.withdrawal_dict(w) for w in rows]


@router.post("/{withdrawal_id}/cancel")
async def cancel_withdrawal(withdrawal_id: UUID, db: AsyncSession = Depends(get_db),
                            current_user=Depends(require_roles(*ROLES))):
    try:
        w = await wsvc.cancel(db, withdrawal_id=withdrawal_id, user=current_user)
    except FinanceError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    await db.commit()
    return wsvc.withdrawal_dict(w)
