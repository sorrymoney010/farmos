"""Revenue adapter contract (spec §14 / §57.10).

Adapters describe earning sources; they never move money. Every adapter is
registered in ``revenue_adapters`` and can be killed from admin without a
deploy (``enabled`` flag checked at job creation, dispatch and scheduling).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.services import profitability as prof


@dataclass
class OpportunitySpec:
    external_id: str
    name: str
    category: str
    workload_type: str
    asset_symbol: str = "USD"
    revenue_usd_per_job: Optional[float] = None
    reward_asset_per_job: Optional[float] = None
    estimated_revenue_usd_hour: Optional[float] = None
    expected_runtime_seconds: Optional[int] = None
    thermal_intensity: float = 0.3
    payment_confidence: float = 0.9
    repeatability_score: float = 0.8
    expected_payment_days: float = 0.0
    risk_score: float = 0.1
    normalized_resource_consumption: float = 0.5
    estimated_api_cost_usd: float = 0.0
    expected_network_bytes: int = 0
    min_farm_score: Optional[float] = None
    resource_requirements: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    payload_template: dict[str, Any] = field(default_factory=dict)
    available_capacity: Optional[int] = None
    expires_at: Optional[datetime] = None


@dataclass
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class JobSpec:
    workload_type: str
    payload: dict[str, Any]
    max_runtime_seconds: Optional[int] = None
    constraints: dict[str, Any] = field(default_factory=dict)
    priority: int = 100


@dataclass
class VerificationResult:
    status: str               # DELEGATED | PASSED | FAILED
    method: str
    detail: str = ""


@dataclass
class ClaimResult:
    status: str               # NOT_APPLICABLE | SIMULATED_CLAIMED | FAILED
    amount_asset: float = 0.0
    asset_symbol: str = "USD"
    fee_usd: float = 0.0
    receipt_id: Optional[str] = None
    simulated: bool = True


@dataclass
class ReconciliationResult:
    status: str               # MATCHED | MISMATCH | NOT_APPLICABLE
    expected_asset: float = 0.0
    external_asset: float = 0.0
    difference_asset: float = 0.0
    detail: str = ""
    simulated: bool = True


class RevenueAdapter(ABC):
    id: str = ""
    name: str = ""
    category: str = ""
    kind: str = "INTERNAL"          # INTERNAL | EXTERNAL
    simulated: bool = False
    certification_stage: str = "SANDBOX"

    @abstractmethod
    async def discover(self) -> list[OpportunitySpec]: ...

    async def estimate(self, terms: prof.OpportunityTerms, device: prof.DeviceProfile,
                       policy: prof.CostPolicy, **kw: Any) -> prof.RevenueEstimate:
        return prof.estimate_job(terms, device, policy, **kw)

    @abstractmethod
    async def validate(self, opportunity: Any) -> ValidationResult: ...

    @abstractmethod
    async def build_job(self, opportunity: Any, overrides: dict[str, Any] | None = None) -> JobSpec: ...

    async def verify(self, job: Any, result: Any) -> VerificationResult:
        # Result integrity is enforced by the platform VerificationService (Phase 2);
        # adapters only add adapter-native proofs on top.
        return VerificationResult("DELEGATED", "platform VerificationService")

    @abstractmethod
    async def claim(self, confirmed_work: list[dict[str, Any]]) -> ClaimResult: ...

    @abstractmethod
    async def reconcile(self, expected_asset: float) -> ReconciliationResult: ...

    async def health_check(self) -> dict[str, Any]:
        return {"status": "OK"}

    async def get_constraints(self) -> dict[str, Any]:
        return {}

    async def get_balance(self) -> dict[str, Any]:
        return {"available": None, "note": "no external account"}
