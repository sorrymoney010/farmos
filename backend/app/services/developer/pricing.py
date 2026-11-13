"""Customer price engine + quotes (spec §170-§173, built on the profitability engine).

    fixed      = device-side costs from estimate_job() at gross 0 (power, wear,
                 network, infrastructure) on the reference device, with the
                 runtime scaled to the payload size
    variable   = payout-fee share + failure probability (costs proportional to price)
    verify     = PRICING_VERIFICATION_USD (+ redundant re-execution share for
                 monitoring workloads)
    risk       = fixed * PRICING_RISK_PREMIUM
    price      = max((fixed + verify + risk) * (1 + margin) * tier * urgency / (1 - variable),
                     PRICING_MIN_PRICE_USD)

The margin floor (spec §172) is enforced: contribution margin =
price * (1 - variable) - (fixed + verify + risk) must be >= PRICING_MIN_CONTRIBUTION_MARGIN
* price, and the job must be profitable under estimate_job() at that price.
FCU (spec §170) = runtime minutes x CPU share x RAM factor; it is informative
and never tied to a token.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.developer import PriceQuote
from app.services import opportunity_service as opps
from app.services import profitability as prof

MONITORING = {"http_check", "monitor"}
BASE_RUNTIME_PAYLOAD = {"checksum": 262144, "compression": 131072}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def spec_hash(workload_type: str, payload: dict, resources: dict, priority: int) -> str:
    blob = json.dumps({"w": workload_type, "p": payload, "r": resources, "pr": priority},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _runtime_scale(workload_type: str, payload: dict) -> float:
    if workload_type in BASE_RUNTIME_PAYLOAD:
        size = float(payload.get("size_bytes") or BASE_RUNTIME_PAYLOAD[workload_type])
        rounds = float(payload.get("rounds") or 1)
        return min(max(size * rounds / BASE_RUNTIME_PAYLOAD[workload_type], 0.25), 64.0)
    if workload_type == "monitor":
        return max(len(payload.get("targets") or []), 1) / 3.0
    if workload_type == "json_transform":
        return min(max(len(json.dumps(payload.get("document", []))) / 2048.0, 0.5), 20.0)
    return 1.0


def _ceil6(x: float) -> float:
    return math.ceil(x * 1e6) / 1e6


async def compute_price(db: AsyncSession, workload_type: str, payload: dict, resources: dict,
                        priority: int) -> dict[str, Any]:
    opp = await opps.internal_opportunity(db, workload_type)
    terms = opps.terms_from(opp, workload_type, resources)
    base_rt = float(terms.expected_runtime_seconds or prof.WORKLOAD_DEFAULT_RUNTIME_S.get(workload_type, 30))
    scale = _runtime_scale(workload_type, payload)
    terms.expected_runtime_seconds = int(max(1, round(base_rt * scale)))
    if workload_type == "monitor":
        terms.expected_network_bytes = int((terms.expected_network_bytes or 65536) * scale)
    policy = await opps.cost_policy(db)
    device = prof.DeviceProfile()
    zero = prof.estimate_job(terms, device, policy, gross_override=0.0)
    fixed = zero.total_cost_usd
    fee_pct = policy.payout_fee_pct if terms.payout_fee_pct is None else float(terms.payout_fee_pct)
    variable = min(fee_pct + zero.failure_probability, 0.9)
    verify = settings.PRICING_VERIFICATION_USD + (fixed * settings.REDUNDANT_SAMPLE_RATE
                                                 if workload_type in MONITORING else 0.0)
    risk = fixed * settings.PRICING_RISK_PREMIUM
    base = fixed + verify + risk
    tier = str((resources or {}).get("tier", "STANDARD")).upper()
    tier_mult = prof.TIER_MULTIPLIER.get(tier, 1.0)
    urgent = priority >= settings.PRICING_URGENT_PRIORITY
    urgency_mult = 1.0 + (settings.PRICING_URGENCY_PREMIUM if urgent else 0.0)
    raw = base * (1 + settings.PRICING_TARGET_MARGIN) * tier_mult * urgency_mult / (1 - variable)
    price = max(raw, settings.PRICING_MIN_PRICE_USD)
    # margin floor: raise the price until the contribution margin clears the floor
    floor = settings.PRICING_MIN_CONTRIBUTION_MARGIN
    if (1 - variable - floor) > 0:
        price = max(price, base / (1 - variable - floor))
    price = _ceil6(price)
    est = prof.estimate_job(terms, device, policy, gross_override=price)
    if not est.profitable:  # pragma: no cover - guarded by the floor above
        price = _ceil6(max(price, est.total_cost_usd * 1.5))
        est = prof.estimate_job(terms, device, policy, gross_override=price)
    contribution = price * (1 - variable) - base
    req = prof.requirements_for(terms, resources)
    runtime = est.runtime_seconds
    fcu = round(runtime / 60.0 * max(req["cpu_pct"], 1.0) / 25.0 * max(1.0, req["ram_mb"] / 256.0), 6)
    maximum = _ceil6(price * (1 + settings.PRICING_RESERVE_BUFFER))
    return {
        "estimated_cost_usd": price, "maximum_cost_usd": maximum, "estimated_runtime_seconds": round(runtime, 3),
        "fcu": fcu, "currency": "USD",
        "breakdown": {"device_costs_usd": round(fixed, 10), "verification_usd": round(verify, 10),
                      "risk_usd": round(risk, 10), "variable_share": round(variable, 6),
                      "target_margin": settings.PRICING_TARGET_MARGIN, "tier": tier, "tier_multiplier": tier_mult,
                      "urgent": urgent, "urgency_multiplier": urgency_mult,
                      "minimum_price_usd": settings.PRICING_MIN_PRICE_USD,
                      "contribution_margin_usd": round(contribution, 10),
                      "contribution_margin_pct": round(contribution / price, 6) if price else 0.0,
                      "margin_floor_pct": floor, "expected_net_usd": round(est.expected_net_profit_usd, 10),
                      "cost_detail": {k: round(v, 10) for k, v in zero.costs.items()}},
    }


async def create_quote(db: AsyncSession, customer_id: uuid.UUID, workload_type: str, payload: dict,
                       resources: dict, priority: int) -> PriceQuote:
    p = await compute_price(db, workload_type, payload, resources, priority)
    q = PriceQuote(id=uuid.uuid4(), customer_id=customer_id,
                   spec_hash=spec_hash(workload_type, payload, resources, priority), workload_type=workload_type,
                   estimated_cost_usd=p["estimated_cost_usd"], maximum_cost_usd=p["maximum_cost_usd"],
                   estimated_runtime_seconds=p["estimated_runtime_seconds"], fcu=p["fcu"],
                   breakdown=p["breakdown"], expires_at=utcnow() + timedelta(seconds=settings.QUOTE_TTL_SECONDS))
    db.add(q)
    await db.flush()
    return q


def quote_dict(q: PriceQuote, runs: int | None = None) -> dict[str, Any]:
    d = {"quote_id": str(q.id), "workload_type": q.workload_type,
         "estimated_cost_usd": f"{float(q.estimated_cost_usd):.6f}",
         "maximum_cost_usd": f"{float(q.maximum_cost_usd):.6f}",
         "estimated_runtime_seconds": float(q.estimated_runtime_seconds), "fcu": float(q.fcu),
         "expires_at": q.expires_at.isoformat(), "breakdown": q.breakdown, "per": "run"}
    if runs:
        d["runs"] = runs
        d["estimated_total_usd"] = f"{float(q.estimated_cost_usd) * runs:.6f}"
    return d
