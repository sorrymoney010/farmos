"""Simulated payout provider: a database-backed fake chain. It never opens a
network connection, holds no keys and moves no real value. Balances are kept
in "sim units" (1e-12 USD) so sub-cent ledger amounts reconcile exactly; payout
instructions use USDC atomic units (6 decimals)."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models.finance import SimChainBalance, SimChainTx
from app.payouts.base import FORBIDDEN_CHAIN_IDS, ChainStatus, PayoutProvider, ProviderRejected, SignedPayout

SIM_UNITS = Decimal(10) ** 12
FAIL_PAYOUT_IDS: set[str] = set()  # test hook: these payouts fail on-chain


def to_sim_units(amount: Decimal) -> int:
    return int((Decimal(str(amount)) * SIM_UNITS).to_integral_value())


def from_sim_units(units: int) -> Decimal:
    return (Decimal(int(units)) / SIM_UNITS).quantize(Decimal("0.000000000001"))


def atomic_to_sim(amount_atomic: int, decimals: int) -> int:
    return int(amount_atomic) * 10 ** (12 - decimals)


class SimulatedPayoutProvider(PayoutProvider):
    name = "simulated"
    simulated = True
    network = "base-sepolia"

    def _check_chain(self, network: str, chain_id: int) -> None:
        if chain_id in FORBIDDEN_CHAIN_IDS:
            raise ProviderRejected("MAINNET_REFUSED", f"chain {chain_id}")
        if settings.PAYOUT_NETWORKS.get(network) != chain_id:
            raise ProviderRejected("UNSUPPORTED_NETWORK", network)

    async def _adjust(self, db, address: str, asset: str, delta_units: int) -> int:
        await db.execute(pg_insert(SimChainBalance).values(network=self.network, address=address.lower(), asset=asset,
                                                           balance_atomic=0)
                         .on_conflict_do_nothing(constraint="uq_sim_chain_balance"))
        row = (await db.execute(select(SimChainBalance).where(
            SimChainBalance.network == self.network, SimChainBalance.address == address.lower(),
            SimChainBalance.asset == asset).with_for_update().execution_options(populate_existing=True))).scalar_one()
        if row.balance_atomic + delta_units < 0:
            raise ProviderRejected("INSUFFICIENT_FUNDS", f"{address} has {from_sim_units(row.balance_atomic)}", retryable=True)
        row.balance_atomic += delta_units
        return row.balance_atomic

    async def deposit(self, db, address: str, asset: str, amount: Decimal, reference: str) -> str:
        """Simulate funds arriving at a treasury address (e.g. simulated billing settlement)."""
        units = to_sim_units(amount)
        tx_hash = "0x" + hashlib.sha256(f"sim-deposit|{reference}".encode()).hexdigest()
        if (await db.execute(select(SimChainTx.tx_hash).where(SimChainTx.tx_hash == tx_hash))).first():
            return tx_hash  # idempotent
        await self._adjust(db, address, asset, units)
        db.add(SimChainTx(tx_hash=tx_hash, network=self.network, kind="DEPOSIT", to_address=address.lower(),
                          asset=asset, amount_atomic=units, status="CONFIRMED", reference=reference[:100]))
        await db.flush()
        return tx_hash

    async def broadcast(self, signed: SignedPayout, *, db) -> str:
        ins = signed.instruction
        self._check_chain(ins.network, ins.chain_id)
        if not signed.signature.startswith("mock-sig:"):
            raise ProviderRejected("BAD_SIGNATURE")
        tx_hash = "0x" + hashlib.sha256(f"sim-payout|{ins.payout_id}".encode()).hexdigest()
        if (await db.execute(select(SimChainTx.tx_hash).where(SimChainTx.tx_hash == tx_hash))).first():
            raise ProviderRejected("DUPLICATE_BROADCAST", tx_hash)
        decimals = settings.TOKEN_REGISTRY[ins.asset][ins.network]["decimals"]
        units = atomic_to_sim(ins.amount_atomic, decimals)
        await self._adjust(db, settings.PAYOUT_HOT_WALLET_ADDRESS, ins.asset, -units)
        await self._adjust(db, ins.recipient, ins.asset, units)
        db.add(SimChainTx(tx_hash=tx_hash, network=self.network, kind="TRANSFER",
                          from_address=settings.PAYOUT_HOT_WALLET_ADDRESS.lower(), to_address=ins.recipient.lower(),
                          asset=ins.asset, amount_atomic=units, status="PENDING", reference=ins.payout_id))
        await db.flush()
        return tx_hash

    async def status(self, tx_hash: str, *, db) -> ChainStatus:
        tx = (await db.execute(select(SimChainTx).where(SimChainTx.tx_hash == tx_hash).with_for_update())).scalar_one_or_none()
        if tx is None:
            return ChainStatus("FAILED", detail="unknown tx")
        if tx.status in ("CONFIRMED", "FAILED"):
            return ChainStatus(tx.status, tx.polls, tx.block_number)
        tx.polls += 1
        if tx.block_number is None:
            n = (await db.execute(select(func.count()).select_from(SimChainTx))).scalar() or 0
            tx.block_number = 1_000_000 + int(n)
        if tx.reference in FAIL_PAYOUT_IDS:
            # simulated revert: funds return to the hot wallet
            await self._adjust(db, tx.to_address, tx.asset, -tx.amount_atomic)
            await self._adjust(db, tx.from_address, tx.asset, tx.amount_atomic)
            tx.status = "FAILED"
            return ChainStatus("FAILED", tx.polls, tx.block_number, "simulated revert")
        need = max(1, settings.PAYOUT_CONFIRMATIONS_REQUIRED)
        if tx.polls >= need:
            tx.status = "CONFIRMED"
            return ChainStatus("CONFIRMED", tx.polls, tx.block_number)
        return ChainStatus("SEEN_ONCHAIN" if tx.polls == 1 else "CONFIRMING", tx.polls, tx.block_number)

    async def balance(self, address: str, asset: str, *, db) -> Decimal:
        row = (await db.execute(select(SimChainBalance.balance_atomic).where(
            SimChainBalance.network == self.network, SimChainBalance.address == address.lower(),
            SimChainBalance.asset == asset))).scalar()
        return from_sim_units(row or 0)
