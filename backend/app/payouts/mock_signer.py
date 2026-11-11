"""Key-less mock signer: enforces spec §108 rejection rules, produces a
deterministic hash "signature". There is no private key."""

from __future__ import annotations

import hashlib
import threading

from app.payouts.base import (
    FORBIDDEN_CHAIN_IDS, PayoutInstruction, PayoutSigner, SignedPayout, SignerAttestation, SignerRejected,
    approval_hash,
)


class MockSigner(PayoutSigner):
    name = "mock"
    _singleton: "MockSigner | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._signed: set[str] = set()

    @classmethod
    def instance(cls) -> "MockSigner":
        with cls._lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    def check(self, ins: PayoutInstruction, att: SignerAttestation) -> None:
        if att.emergency_stop:
            raise SignerRejected("EMERGENCY_STOP", "payouts are paused", retryable=True)
        if ins.chain_id in FORBIDDEN_CHAIN_IDS:
            raise SignerRejected("UNSUPPORTED_CHAIN", f"mainnet chain {ins.chain_id} is never signed in this build")
        if att.allowed_networks.get(ins.network) != ins.chain_id:
            raise SignerRejected("UNSUPPORTED_CHAIN", f"{ins.network}/{ins.chain_id} not allowlisted")
        if att.token_status != "ACTIVE" or att.token_decimals is None:
            raise SignerRejected("UNSUPPORTED_TOKEN", ins.asset)
        if ins.payout_id in self._signed:
            raise SignerRejected("DUPLICATE_PAYOUT_ID", ins.payout_id)
        if not att.wallet_verified:
            raise SignerRejected("WALLET_NOT_VERIFIED")
        if att.withdrawal_status != "APPROVED":
            raise SignerRejected("WITHDRAWAL_NOT_APPROVED", att.withdrawal_status)
        if ins.recipient.lower() != att.wallet_address.lower():
            raise SignerRejected("RECIPIENT_MISMATCH")
        if ins.amount_atomic != att.approved_amount_atomic or ins.amount_atomic <= 0:
            raise SignerRejected("AMOUNT_MISMATCH")
        expected = approval_hash(withdrawal_id=ins.withdrawal_id, provider_id=ins.provider_id, recipient=ins.recipient,
                                 amount_atomic=ins.amount_atomic, asset=ins.asset, chain_id=ins.chain_id,
                                 approvers=list(att.approvers))
        if expected != ins.approval_hash:
            raise SignerRejected("APPROVAL_HASH_MISMATCH")
        if att.max_tx_atomic and ins.amount_atomic > att.max_tx_atomic:
            raise SignerRejected("TRANSACTION_LIMIT_EXCEEDED")
        if att.daily_limit_atomic and att.daily_total_atomic_before + ins.amount_atomic > att.daily_limit_atomic:
            raise SignerRejected("DAILY_LIMIT_EXCEEDED", retryable=True)

    def sign(self, instruction: PayoutInstruction, attestation: SignerAttestation) -> SignedPayout:
        self.check(instruction, attestation)
        digest = hashlib.sha256(("MOCK-SIGNER-NO-KEY|" + instruction.canonical()).encode()).hexdigest()
        self._signed.add(instruction.payout_id)
        return SignedPayout(instruction=instruction, signature=f"mock-sig:{digest}", signer=self.name)
