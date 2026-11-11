"""Wallet endpoints: EIP-191 / SIWE ownership verification (spec §21, §22, §27).

Users never send a seed phrase or private key. They sign a non-transaction
message in their own wallet; the backend recovers the address.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.core import Wallet
from app.db.session import get_db
from app.dependencies import get_current_user
from app.services.finance import wallets as wsvc
from app.services.finance.controls import FinanceError

router = APIRouter()


class ChallengeRequest(BaseModel):
    address: str = Field(min_length=40, max_length=64)
    network: str = "base-sepolia"
    label: str | None = Field(default=None, max_length=100)


class VerifyRequest(BaseModel):
    challenge_id: UUID
    signature: str = Field(min_length=130, max_length=140)


def _err(exc: FinanceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/challenge")
async def wallet_challenge(req: ChallengeRequest, db: AsyncSession = Depends(get_db),
                           current_user=Depends(get_current_user)):
    """Step 1: get a one-time SIWE message to sign with ``personal_sign``."""
    try:
        wallet, ch = await wsvc.issue_challenge(db, user=current_user, address=req.address, network=req.network,
                                                label=req.label)
    except FinanceError as exc:
        raise _err(exc)
    await db.commit()
    return {"challenge_id": str(ch.id), "wallet_id": str(wallet.id), "address": wallet.address,
            "network": wallet.network, "chain_id": ch.chain_id, "message": ch.message,
            "expires_at": ch.expires_at.isoformat(), "method": wsvc.METHOD,
            "note": "Sign this message in your wallet. It does not authorize any transfer. Never share a seed phrase."}


@router.post("/verify")
async def wallet_verify(req: VerifyRequest, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_user)):
    """Step 2: submit the signature; the backend recovers and compares the address."""
    try:
        ok, wallet, error = await wsvc.verify(db, user=current_user, challenge_id=req.challenge_id,
                                              signature=req.signature)
    except FinanceError as exc:
        await db.rollback()
        raise _err(exc)
    await db.commit()  # persist failed-attempt counters as well as successes
    if not ok:
        raise HTTPException(status_code=400, detail=f"verification failed: {error}")
    return {"detail": "Wallet verified", **wsvc.wallet_dict(wallet)}


@router.post("/{wallet_id}/default")
async def wallet_set_default(wallet_id: UUID, db: AsyncSession = Depends(get_db),
                             current_user=Depends(get_current_user)):
    try:
        w = await wsvc.set_default(db, user=current_user, wallet_id=wallet_id)
    except FinanceError as exc:
        raise _err(exc)
    await db.commit()
    return wsvc.wallet_dict(w)


@router.get("/")
async def list_wallets(db: AsyncSession = Depends(get_db), current_user=Depends(get_current_user)):
    rows = (await db.execute(select(Wallet).where(Wallet.user_id == current_user.id)
                             .order_by(Wallet.created_at))).scalars().all()
    return [wsvc.wallet_dict(w) for w in rows]
