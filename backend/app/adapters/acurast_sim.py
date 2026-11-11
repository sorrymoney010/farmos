"""SIMULATED external revenue adapter modelled on spec §80 "acurast".

This adapter never contacts Acurast or any chain, holds no wallet, keys or
credentials, and moves no money. It produces deterministic, clearly-labelled
SIMULATED opportunities so the profitability engine, scheduler and kill switch
can be exercised end-to-end:

* rewards are denominated in a token (``ACU``), valued only through the
  configured price snapshot with volatility + liquidity haircuts
  (never at face value), with claim/chain fees and slow payment;
* the simulated work is executed as deterministic internal CPU jobs so the
  Phase 2 verifier can check every result;
* claim() returns a simulated receipt; reconcile() compares the expected token
  total with a simulated external balance (``SIM_BALANCE_SHORTFALL`` lets tests
  model a counterparty that under-pays, which trips the §126 runbook).
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from app.adapters.base import (
    ClaimResult, JobSpec, OpportunitySpec, ReconciliationResult, RevenueAdapter, ValidationResult,
)
from app.workloads import cpu_data

SIM_BALANCE_SHORTFALL = 0.0      # fraction of expected tokens the simulated counterparty fails to pay
RECONCILE_TOLERANCE = 0.01       # 1%
CLAIM_FEE_USD = 0.02             # simulated per-claim chain fee
JOBS_PER_CLAIM = 100


class AcurastSimAdapter(RevenueAdapter):
    id = "acurast_sim"
    name = "Acurast (SIMULATED — no network, no wallet)"
    category = "depin_compute"
    kind = "EXTERNAL"
    simulated = True
    certification_stage = "SANDBOX"

    async def discover(self) -> list[OpportunitySpec]:
        common = dict(asset_symbol="ACU", expected_payment_days=14, payment_confidence=0.6, risk_score=0.4,
                      chain_fee_usd_per_job=CLAIM_FEE_USD / JOBS_PER_CLAIM)
        return [
            OpportunitySpec(
                external_id="acu-tee-compute", name="[SIM] Acurast TEE compute slot", category="depin_compute",
                workload_type="checksum", reward_asset_per_job=0.25, expected_runtime_seconds=60,
                thermal_intensity=0.4, repeatability_score=0.7, normalized_resource_consumption=0.6,
                expected_network_bytes=8192, min_farm_score=10, available_capacity=50,
                payload_template={"size_bytes": 1048576, "rounds": 2},
                asset_symbol=common["asset_symbol"], expected_payment_days=common["expected_payment_days"],
                payment_confidence=common["payment_confidence"], risk_score=common["risk_score"],
                constraints={"chain_fee_usd_per_job": common["chain_fee_usd_per_job"], "simulated": True,
                             "withdrawal_minimum_asset": 100, "legal_notes": "simulation only"},
            ),
            OpportunitySpec(
                external_id="acu-token-mining", name="[SIM] Token mining (low yield)", category="token_mining",
                workload_type="checksum", reward_asset_per_job=0.01, expected_runtime_seconds=300,
                thermal_intensity=0.9, repeatability_score=0.9, normalized_resource_consumption=1.0,
                expected_network_bytes=1024, available_capacity=1000,
                payload_template={"size_bytes": 8388608, "rounds": 16},
                asset_symbol="ACU", expected_payment_days=30, payment_confidence=0.5, risk_score=0.6,
                constraints={"chain_fee_usd_per_job": common["chain_fee_usd_per_job"], "simulated": True},
            ),
        ]

    async def validate(self, opportunity: Any) -> ValidationResult:
        reasons = []
        if not opportunity.asset_symbol or opportunity.asset_symbol.upper() in ("USD", "USDC"):
            reasons.append("token-denominated opportunity expected")
        if (opportunity.constraints or {}).get("reward_asset_per_job") in (None, 0):
            reasons.append("no per-job token reward")
        if opportunity.workload_type not in cpu_data.CPU_DATA_WORKLOADS:
            reasons.append("simulated work must be verifiable CPU/data work")
        return ValidationResult(not reasons, reasons)

    async def build_job(self, opportunity: Any, overrides: dict[str, Any] | None = None) -> JobSpec:
        payload = {**(opportunity.payload_template or {}), **(overrides or {})}
        payload.setdefault("seed", secrets.token_hex(8))
        cpu_data.validate_payload(opportunity.workload_type, payload)
        return JobSpec(workload_type=opportunity.workload_type, payload=payload,
                       max_runtime_seconds=opportunity.expected_runtime_seconds)

    async def claim(self, confirmed_work: list[dict[str, Any]]) -> ClaimResult:
        amount = sum(float(w.get("amount_asset", 0)) for w in confirmed_work)
        rid = "SIM-" + hashlib.sha256(repr(sorted(str(w.get("job_id")) for w in confirmed_work)).encode()).hexdigest()[:16]
        return ClaimResult("SIMULATED_CLAIMED", amount, "ACU", CLAIM_FEE_USD if confirmed_work else 0.0, rid, True)

    async def reconcile(self, expected_asset: float) -> ReconciliationResult:
        external = expected_asset * (1 - SIM_BALANCE_SHORTFALL)
        diff = expected_asset - external
        ok = expected_asset == 0 or abs(diff) / expected_asset <= RECONCILE_TOLERANCE
        return ReconciliationResult("MATCHED" if ok else "MISMATCH", expected_asset, external, diff,
                                    "simulated external balance", True)

    async def health_check(self) -> dict[str, Any]:
        return {"status": "OK", "simulated": True, "note": "no external endpoint contacted"}

    async def get_constraints(self) -> dict[str, Any]:
        return {"asset": "ACU", "withdrawal_minimum_asset": 100, "jobs_per_claim": JOBS_PER_CLAIM,
                "claim_fee_usd": CLAIM_FEE_USD}

    async def get_balance(self) -> dict[str, Any]:
        return {"available": None, "simulated": True, "note": "no wallet is held by this adapter"}


def _new_seed() -> str:  # pragma: no cover - helper for scripts
    return secrets.token_hex(8)
