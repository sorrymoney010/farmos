"""Wallet ownership verification (spec §21, §22, §27; DoD 14: no private keys).

Flow: backend issues a one-time nonce inside an EIP-4361 (SIWE) message ->
the user signs it with EIP-191 ``personal_sign`` in their own wallet (a
non-transaction message: "This signature does not authorize a transfer.") ->
backend recovers the signer address and compares it with the claimed address.

* Nonces are single-use (replay -> 409), expire, and lock after repeated failures.
* The message is stored server-side; the client only returns a signature.
* Adding/switching a payout address starts a security cooldown (spec §27/§149).
* Users never provide seed phrases or private keys; the server never sees one.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, is_checksum_address, to_checksum_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.core import Provider, Wallet, WalletVerificationChallenge
from app.db.models.enums import AuditEventType, WalletChainFamily
from app.services.finance.controls import FinanceError

METHOD = "EIP-191/SIWE"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_address(address: str) -> str:
    a = (address or "").strip()
    if not is_address(a):
        raise FinanceError("not a valid EVM address", 400)
    if a != a.lower() and a[2:] != a[2:].upper() and not is_checksum_address(a):
        raise FinanceError("address fails EIP-55 checksum", 400)
    return to_checksum_address(a)


def siwe_message(*, address: str, chain_id: int, nonce: str, account_ref: str, issued_at: datetime,
                 expires_at: datetime, request_id: str) -> str:
    """EIP-4361 message carrying the spec §21 ownership statement."""
    statement = (f"FARMOS Wallet Verification. Account: {account_ref}. Purpose: Verify wallet ownership. "
                 "This signature does not authorize a transfer.")
    return (f"{settings.WALLET_SIWE_DOMAIN} wants you to sign in with your Ethereum account:\n"
            f"{address}\n\n{statement}\n\n"
            f"URI: {settings.WALLET_SIWE_URI}\nVersion: 1\nChain ID: {chain_id}\nNonce: {nonce}\n"
            f"Issued At: {issued_at.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"Expiration Time: {expires_at.strftime('%Y-%m-%dT%H:%M:%SZ')}\nRequest ID: {request_id}")


def recover_signer(message: str, signature: str) -> str:
    sig = (signature or "").strip()
    raw = sig[2:] if sig.startswith("0x") else sig
    if len(raw) != 130:
        raise FinanceError("signature must be 65 bytes hex", 400)
    try:
        return Account.recover_message(encode_defunct(text=message), signature=bytes.fromhex(raw))
    except Exception:
        raise FinanceError("signature could not be recovered", 400)


async def provider_of(db: AsyncSession, user_id: uuid.UUID) -> Optional[Provider]:
    return (await db.execute(select(Provider).where(Provider.user_id == user_id))).scalars().first()


async def issue_challenge(db: AsyncSession, *, user, address: str, network: str, label: str | None,
                          now: datetime | None = None) -> tuple[Wallet, WalletVerificationChallenge]:
    now = now or utcnow()
    chain_id = settings.WALLET_NETWORKS.get(network)
    if chain_id is None:
        raise FinanceError(f"unsupported network {network}", 400)
    addr = normalize_address(address)
    provider = await provider_of(db, user.id)
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id, Wallet.network == network,
                                                    Wallet.address == addr))).scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(user_id=user.id, provider_id=provider.id if provider else None,
                        chain_family=WalletChainFamily.EVM, network=network, address=addr,
                        label=label or "Wallet", is_verified=False, is_default=False, chain_id=chain_id)
        db.add(wallet)
        await db.flush()
    elif label:
        wallet.label = label
    challenge_id = uuid.uuid4()
    nonce = secrets.token_hex(16)
    account_ref = f"FARM-PROVIDER-{str(provider.id)[:8].upper()}" if provider else f"FARM-USER-{str(user.id)[:8].upper()}"
    expires = now + timedelta(seconds=settings.WALLET_CHALLENGE_TTL_SECONDS)
    ch = WalletVerificationChallenge(
        id=challenge_id, wallet_id=wallet.id, nonce=nonce, user_id=user.id, chain_id=chain_id, expires_at=expires,
        message=siwe_message(address=addr, chain_id=chain_id, nonce=nonce, account_ref=account_ref,
                             issued_at=now, expires_at=expires, request_id=str(challenge_id)))
    db.add(ch)
    db.add(AuditLog(event_type=AuditEventType.WALLET_CHALLENGE_ISSUED, actor_type="USER", actor_id=user.id,
                    target_type="WALLET", target_id=wallet.id, event_metadata={"network": network, "address": addr}))
    await db.flush()
    return wallet, ch


async def verify(db: AsyncSession, *, user, challenge_id: uuid.UUID, signature: str,
                 now: datetime | None = None) -> tuple[bool, Wallet, str | None]:
    """Returns (ok, wallet, error). Failures are recorded (caller commits) then reported."""
    now = now or utcnow()
    ch = (await db.execute(select(WalletVerificationChallenge).where(WalletVerificationChallenge.id == challenge_id)
                           .with_for_update())).scalar_one_or_none()
    if ch is None or ch.user_id != user.id:
        raise FinanceError("challenge not found", 404)
    wallet = (await db.execute(select(Wallet).where(Wallet.id == ch.wallet_id).with_for_update())).scalar_one()
    if ch.consumed_at is not None:
        raise FinanceError("challenge already used (replay rejected)", 409)
    if now >= ch.expires_at:
        ch.consumed_at = now
        return False, wallet, "challenge expired"
    try:
        recovered = recover_signer(ch.message, signature)
    except FinanceError as exc:
        recovered, err = None, exc.detail
    else:
        err = None if recovered.lower() == wallet.address.lower() else "signature is not from this address"
    if err:
        ch.failed_attempts = int(ch.failed_attempts or 0) + 1
        if ch.failed_attempts >= settings.WALLET_MAX_FAILED_ATTEMPTS:
            ch.consumed_at = now
        db.add(AuditLog(event_type=AuditEventType.WALLET_VERIFICATION_FAILED, actor_type="USER", actor_id=user.id,
                        target_type="WALLET", target_id=wallet.id,
                        event_metadata={"reason": err, "attempts": ch.failed_attempts}))
        return False, wallet, err

    ch.consumed_at = now
    provider = await provider_of(db, user.id)
    others = []
    if provider is not None:
        wallet.provider_id = provider.id
        others = (await db.execute(select(Wallet).where(Wallet.provider_id == provider.id, Wallet.id != wallet.id,
                                                        Wallet.is_verified.is_(True)))).scalars().all()
    was_verified = wallet.is_verified
    wallet.is_verified = True
    wallet.verified_at = now
    wallet.verification_method = METHOD
    wallet.chain_id = ch.chain_id
    if others and not was_verified:
        # a new payout-capable address on an existing account: security cooldown (spec §27)
        wallet.cooldown_until = now + timedelta(hours=settings.WALLET_CHANGE_COOLDOWN_HOURS)
    if provider is not None and not any(o.is_default for o in others):
        wallet.is_default = True
    db.add(AuditLog(event_type=AuditEventType.WALLET_VERIFIED, actor_type="USER", actor_id=user.id,
                    target_type="WALLET", target_id=wallet.id,
                    event_metadata={"address": wallet.address, "network": wallet.network, "method": METHOD,
                                    "cooldown_until": wallet.cooldown_until.isoformat() if wallet.cooldown_until else None}))
    return True, wallet, None


async def set_default(db: AsyncSession, *, user, wallet_id: uuid.UUID, now: datetime | None = None) -> Wallet:
    now = now or utcnow()
    provider = await provider_of(db, user.id)
    if provider is None:
        raise FinanceError("only providers have payout wallets", 403)
    wallets = (await db.execute(select(Wallet).where(Wallet.provider_id == provider.id).with_for_update())).scalars().all()
    target = next((w for w in wallets if w.id == wallet_id), None)
    if target is None:
        raise FinanceError("wallet not found", 404)
    if not target.is_verified:
        raise FinanceError("wallet must be verified first", 409)
    current = next((w for w in wallets if w.is_default), None)
    if current is not None and current.id != target.id:
        cd = now + timedelta(hours=settings.WALLET_CHANGE_COOLDOWN_HOURS)
        target.cooldown_until = max(target.cooldown_until or cd, cd)
        db.add(AuditLog(event_type=AuditEventType.WALLET_DEFAULT_CHANGED, actor_type="USER", actor_id=user.id,
                        target_type="WALLET", target_id=target.id,
                        event_metadata={"from": current.address, "to": target.address,
                                        "cooldown_until": target.cooldown_until.isoformat()}))
    for w in wallets:
        w.is_default = w.id == target.id
    return target


def wallet_dict(w: Wallet, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    return {"wallet_id": str(w.id), "chain_family": w.chain_family.value, "network": w.network,
            "chain_id": w.chain_id, "address": w.address, "label": w.label, "is_verified": w.is_verified,
            "is_default": w.is_default, "verification_method": w.verification_method,
            "verified_at": w.verified_at.isoformat() if w.verified_at else None,
            "cooldown_until": w.cooldown_until.isoformat() if w.cooldown_until else None,
            "in_cooldown": bool(w.cooldown_until and w.cooldown_until > now),
            "payouts_supported": w.network in settings.PAYOUT_NETWORKS}
