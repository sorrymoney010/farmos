"""Payout interfaces. No implementation here touches keys, RPCs or real funds."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Mainnets (Ethereum, Base, Optimism, Polygon, Arbitrum): refused by signer AND provider.
FORBIDDEN_CHAIN_IDS = frozenset({1, 8453, 10, 137, 42161})


def approval_hash(*, withdrawal_id: str, provider_id: str, recipient: str, amount_atomic: int, asset: str,
                  chain_id: int, approvers: list[str]) -> str:
    """Binds an approval to exact payout terms; any change after approval breaks it."""
    payload = {"withdrawal_id": str(withdrawal_id), "provider_id": str(provider_id), "recipient": recipient.lower(),
               "amount_atomic": int(amount_atomic), "asset": asset, "chain_id": int(chain_id),
               "approvers": sorted(str(a) for a in approvers)}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PayoutInstruction:
    """Spec §108 payout request object."""
    payout_id: str
    withdrawal_id: str
    provider_id: str
    asset: str
    network: str
    chain_id: int
    token_contract: Optional[str]
    recipient: str
    amount_atomic: int
    created_at: str
    approval_hash: str
    risk_status: str = "APPROVED"

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SignerAttestation:
    """Facts the signer re-validates independently of the API's decision.
    (In production the signing service would read these itself.)"""
    withdrawal_status: str
    wallet_verified: bool
    wallet_address: str
    approved_amount_atomic: int
    approvers: tuple[str, ...]
    daily_total_atomic_before: int
    emergency_stop: bool
    token_status: Optional[str]
    token_decimals: Optional[int]
    allowed_networks: dict[str, int] = field(default_factory=dict)
    daily_limit_atomic: int = 0
    max_tx_atomic: int = 0


@dataclass(frozen=True)
class SignedPayout:
    instruction: PayoutInstruction
    signature: str
    signer: str


class SignerRejected(Exception):
    def __init__(self, code: str, detail: str = "", retryable: bool = False):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.retryable = code, detail, retryable


class ProviderRejected(Exception):
    def __init__(self, code: str, detail: str = "", retryable: bool = False):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.retryable = code, detail, retryable


class PayoutSigner(ABC):
    name: str = "abstract"

    @abstractmethod
    def sign(self, instruction: PayoutInstruction, attestation: SignerAttestation) -> SignedPayout: ...


@dataclass
class ChainStatus:
    state: str               # PENDING|SEEN_ONCHAIN|CONFIRMING|CONFIRMED|FAILED
    confirmations: int = 0
    block_number: Optional[int] = None
    detail: str = ""


class PayoutProvider(ABC):
    name: str = "abstract"
    simulated: bool = True

    @abstractmethod
    async def broadcast(self, signed: SignedPayout, *, db: Any) -> str: ...

    @abstractmethod
    async def status(self, tx_hash: str, *, db: Any) -> ChainStatus: ...

    @abstractmethod
    async def balance(self, address: str, asset: str, *, db: Any) -> Any: ...
