"""Payout signing + broadcast interfaces (spec §106-§111).

Only isolated, key-less implementations ship in this build:

* ``MockSigner``: enforces every §108 signer rejection rule, signs with a
  deterministic hash (no private key exists anywhere).
* ``SimulatedPayoutProvider``: an in-database simulated chain labelled as a
  testnet; it never contacts an RPC. Mainnet chain ids are hard-refused.

A real signer must live in a separate service (spec §107) and is out of scope.
"""

from app.payouts.base import (  # noqa: F401
    FORBIDDEN_CHAIN_IDS, PayoutInstruction, PayoutProvider, PayoutSigner, ProviderRejected, SignedPayout,
    SignerAttestation, SignerRejected, approval_hash,
)


def get_signer():
    from app.config import settings
    from app.payouts.mock_signer import MockSigner

    if settings.PAYOUT_SIGNER != "mock":
        raise RuntimeError("only the mock signer exists in this build (no real keys)")
    return MockSigner.instance()


def get_provider():
    from app.config import settings
    from app.payouts.simulated import SimulatedPayoutProvider

    if settings.PAYOUT_PROVIDER != "simulated":
        raise RuntimeError("only the simulated payout provider exists in this build (no real funds)")
    return SimulatedPayoutProvider()
