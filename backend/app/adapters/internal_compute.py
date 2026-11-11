"""Internal workload adapter (spec §80 internal_compute + monitoring).

Serves customer-submitted CPU/data jobs (checksum, compression, JSON transform)
and approved-endpoint monitoring. Revenue is the customer's per-job price
(configured here; billed by the Phase 4 customer billing service). No external
accounts, no network calls from the control plane.
"""

from __future__ import annotations

import secrets
from typing import Any

from app.adapters.base import (
    ClaimResult, JobSpec, OpportunitySpec, ReconciliationResult, RevenueAdapter, ValidationResult,
)
from app.workloads import cpu_data

# Customer price list (USD per verified job).
PRICE_LIST = {
    "checksum": 0.002, "compression": 0.002, "json_transform": 0.002,
    "http_check": 0.0025, "api_request": 0.005, "monitor": 0.001, "data_transform": 0.003,
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "checksum": {"size_bytes": 262144, "algorithm": "sha256"},
    "compression": {"size_bytes": 131072},
    "json_transform": {"document": [{"id": i, "v": i * 3} for i in range(20)], "ops": [{"op": "sum", "field": "v"}]},
}


class InternalComputeAdapter(RevenueAdapter):
    id = "internal_compute"
    name = "FARMOS internal compute & monitoring"
    category = "internal_compute"
    kind = "INTERNAL"
    simulated = False
    certification_stage = "APPROVED"

    async def discover(self) -> list[OpportunitySpec]:
        heavy = {"checksum", "compression", "json_transform", "data_transform"}
        out = []
        for wt, price in PRICE_LIST.items():
            out.append(OpportunitySpec(
                external_id=wt,
                name=f"Internal {wt.replace('_', ' ')}",
                category="internal_compute" if wt in heavy else "monitoring",
                workload_type=wt,
                asset_symbol="USD",
                revenue_usd_per_job=price,
                thermal_intensity=0.4 if wt in heavy else 0.1,
                payment_confidence=0.95,
                repeatability_score=0.9,
                expected_payment_days=0,
                risk_score=0.05,
                normalized_resource_consumption=0.5 if wt in heavy else 0.2,
                expected_network_bytes=4096 if wt in heavy else 65536,
                payload_template=TEMPLATES.get(wt, {}),
                constraints={"policy_notes": "customer-approved endpoints only; no scanning"}
                if wt not in heavy else {},
            ))
        return out

    async def validate(self, opportunity: Any) -> ValidationResult:
        reasons = []
        if opportunity.workload_type not in PRICE_LIST:
            reasons.append(f"unsupported workload {opportunity.workload_type}")
        if (opportunity.revenue_usd_per_job or 0) <= 0:
            reasons.append("no customer price")
        return ValidationResult(not reasons, reasons)

    async def build_job(self, opportunity: Any, overrides: dict[str, Any] | None = None) -> JobSpec:
        wt = opportunity.workload_type
        payload = {**(opportunity.payload_template or {}), **(overrides or {})}
        if wt in ("checksum", "compression") and "seed" not in payload:
            payload["seed"] = secrets.token_hex(8)
        if wt in cpu_data.CPU_DATA_WORKLOADS:
            cpu_data.validate_payload(wt, payload)
        elif wt in ("http_check", "api_request") and not str(payload.get("url", "")).startswith(("http://", "https://")):
            raise ValueError("monitoring jobs need an approved http(s) url")
        return JobSpec(workload_type=wt, payload=payload)

    async def claim(self, confirmed_work: list[dict[str, Any]]) -> ClaimResult:
        return ClaimResult("NOT_APPLICABLE", simulated=False)  # customer billing (Phase 4)

    async def reconcile(self, expected_asset: float) -> ReconciliationResult:
        return ReconciliationResult("NOT_APPLICABLE", expected_asset, expected_asset, 0.0,
                                    "internal revenue reconciles against customer billing (Phase 4)", False)
