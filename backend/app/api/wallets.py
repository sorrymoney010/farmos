"""Wallet endpoints for FARMOS."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.core import Wallet
from app.db.models.enums import WalletChainFamily
from app.db.session import get_db
from app.security.signatures import (
    generate_nonce,
    generate_challenge_message,
    verify_sha256,
)
from app.logging import get_logger
from app.dependencies import get_current_user

logger = get_logger(__name__)
router = APIRouter()


class WalletConnectRequest(BaseModel):
    network: str
    chain_family: str
    address: str


class VerifySignatureRequest(BaseModel):
    challenge_id: str
    signature: str


@router.post("/connect-request")
async def connect_wallet_request(
    req: WalletConnectRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        chain = WalletChainFamily(req.chain_family)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unsupported chain family")
    nonce = generate_nonce()
    message = generate_challenge_message(
        account_type="FARMOS-USER",
        account_id=str(current_user.id),
        wallet_address=req.address,
        nonce=nonce,
    )
    challenge_id = str(uuid4())
    # Store challenge in a lightweight in-memory map for demo; use DB in production
    from app.config import settings as _s  # noqa: PLC0415
    if not hasattr(_s, "_wallet_challenges"):
        _s._wallet_challenges = {}
    _s._wallet_challenges[challenge_id] = {
        "nonce": nonce,
        "message": message,
        "user_id": str(current_user.id),
        "chain_family": req.chain_family,
        "network": req.network,
        "address": req.address,
        "expires_at": datetime.now(timezone.utc).timestamp() + 300,
    }
    return {
        "challenge_id": challenge_id,
        "message": message,
        "expires_at": (datetime.now(timezone.utc).timestamp() + 300),
    }


@router.post("/verify-signature")
async def verify_signature(
    req: VerifySignatureRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    challenges = getattr(settings, "_wallet_challenges", {})
    challenge = challenges.get(req.challenge_id)
    if not challenge:
        raise HTTPException(status_code=400, detail="Invalid challenge")
    if challenge["user_id"] != str(current_user.id):
        raise HTTPException(status_code=403, detail="Challenge does not belong to user")
    expected = challenge["message"].encode()
    # In production, recover address from signature and compare to challenge address
    if not verify_sha256(req.signature, "sha256:" + __import__("hashlib").sha256(expected).hexdigest()):
        raise HTTPException(status_code=400, detail="Signature verification failed")
    wallet = Wallet(
        user_id=current_user.id,
        chain_family=WalletChainFamily(challenge["chain_family"]),
        network=challenge["network"],
        address=challenge["address"],
        label="Default Wallet",
        is_verified=True,
        is_default=True,
        verified_at=datetime.now(timezone.utc),
    )
    db.add(wallet)
    await db.commit()
    logger.info("wallet_verified", user_id=str(current_user.id), wallet=str(wallet.id))
    return {"detail": "Wallet verified", "wallet_id": str(wallet.id)}


@router.get("/")
async def list_wallets(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    result = await db.execute(select(Wallet).where(Wallet.user_id == current_user.id))
    wallets = result.scalars().all()
    return [
        {
            "wallet_id": str(w.id),
            "chain_family": w.chain_family.value,
            "network": w.network,
            "address": w.address,
            "is_verified": w.is_verified,
            "is_default": w.is_default,
            "verified_at": w.verified_at.isoformat() if w.verified_at else None,
        }
        for w in wallets
    ]
