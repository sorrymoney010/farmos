"""Profitability engine (spec §17, §64, §65, §146-147). Pure functions.

Every number here is configuration-driven (``settings`` + the admin-editable
``profitability_policy`` override). There are no live market feeds and no
external accounts: token prices come from a configured snapshot and every
non-USD asset is haircut for volatility and liquidity (spec: never rank a
token reward at face value).

estimate_job():
    gross            = per-job reward (asset amount x price) | revenue_usd_per_job | rate/h x runtime
    adjusted_gross   = gross x (1 - volatility_haircut) x (1 - liquidity_haircut)
    costs            = electricity + network + api + chain_fee + payout_fee
                       + infrastructure + depreciation + failure_probability x gross
    net              = adjusted_gross - costs
score_opportunity(): §64 formula. final_score(): §146 (opportunity score x device
fit x reputation x urgency x customer tier). Hard exclusions happen before scoring.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

HEAVY = {"checksum", "compression", "json_transform", "data_transform", "benchmark"}

WORKLOAD_DEFAULT_RUNTIME_S = {
    "http_check": 5, "api_request": 5, "monitor": 15, "checksum": 10, "compression": 10,
    "json_transform": 3, "data_transform": 5, "benchmark": 60,
}
WORKLOAD_DEFAULT_REQUIREMENTS = {
    # cpu_pct, ram_mb, bandwidth_mbps
    "light": {"cpu_pct": 10.0, "ram_mb": 64.0, "bandwidth_mbps": 1.0},
    "heavy": {"cpu_pct": 50.0, "ram_mb": 256.0, "bandwidth_mbps": 0.1},
}
THERMAL_MULTIPLIER = {"GREEN": 1.0, "YELLOW": 0.80, "ORANGE": 0.35, "RED": 0.0, "COOLING": 0.0, "UNKNOWN": 0.6}
TIER_MULTIPLIER = {"STANDARD": 1.0, "PRIORITY": 1.1, "ENTERPRISE": 1.2}


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class CostPolicy:
    power_price_kwh: float = 0.15
    idle_watts: float = 1.5
    active_watts: float = 4.0
    network_cost_per_gb: float = 0.01
    infra_cost_per_job: float = 0.00001
    infra_cost_per_hour: float = 0.001
    payout_fee_pct: float = 0.01
    default_purchase_usd: float = 40.0
    default_resale_usd: float = 8.0
    lifetime_hours: float = 8760.0
    base_failure_probability: float = 0.02
    prices: dict[str, float] = field(default_factory=lambda: {"USD": 1.0, "USDC": 1.0})
    volatility_haircut: dict[str, float] = field(default_factory=dict)
    liquidity_haircut: dict[str, float] = field(default_factory=dict)
    unknown_asset_haircut: float = 0.5

    @classmethod
    def from_settings(cls, s: Any, override: dict[str, Any] | None = None) -> "CostPolicy":
        p = cls(
            power_price_kwh=s.POWER_PRICE_KWH_USD, idle_watts=s.DEVICE_IDLE_WATTS,
            active_watts=s.DEVICE_ACTIVE_WATTS, network_cost_per_gb=s.NETWORK_COST_PER_GB_USD,
            infra_cost_per_job=s.INFRA_COST_PER_JOB_USD, infra_cost_per_hour=s.INFRA_COST_PER_HOUR_USD,
            payout_fee_pct=s.PAYOUT_FEE_PCT, default_purchase_usd=s.DEFAULT_DEVICE_PURCHASE_USD,
            default_resale_usd=s.DEFAULT_DEVICE_RESALE_USD, lifetime_hours=s.DEVICE_EXPECTED_LIFETIME_HOURS,
            base_failure_probability=s.BASE_FAILURE_PROBABILITY, prices=dict(s.PRICE_SNAPSHOT_USD),
            volatility_haircut=dict(s.ASSET_VOLATILITY_HAIRCUT), liquidity_haircut=dict(s.ASSET_LIQUIDITY_HAIRCUT),
            unknown_asset_haircut=s.UNKNOWN_ASSET_HAIRCUT,
        )
        for k, v in (override or {}).items():
            if hasattr(p, k) and v is not None:
                if isinstance(getattr(p, k), dict) and isinstance(v, dict):
                    getattr(p, k).update({str(a): float(b) for a, b in v.items()})
                else:
                    setattr(p, k, float(v))
        return p

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OpportunityTerms:
    adapter_id: str = "internal_compute"
    workload_type: str = "http_check"
    asset_symbol: str = "USD"
    revenue_usd_per_job: Optional[float] = None
    reward_asset_per_job: Optional[float] = None
    estimated_revenue_usd_hour: Optional[float] = None
    expected_runtime_seconds: Optional[float] = None
    thermal_intensity: float = 0.3
    payment_confidence: float = 0.9
    repeatability_score: float = 0.8
    expected_payment_days: float = 0.0
    risk_score: float = 0.1
    normalized_resource_consumption: float = 0.5
    estimated_api_cost_usd: float = 0.0
    expected_network_bytes: int = 0
    chain_fee_usd_per_job: float = 0.0
    payout_fee_pct: Optional[float] = None
    min_farm_score: Optional[float] = None
    requirements: dict[str, float] = field(default_factory=dict)


@dataclass
class DeviceProfile:
    device_id: str = "reference"
    farm_score: Optional[float] = 50.0
    reputation: float = 50.0
    purchase_price_usd: Optional[float] = None
    resale_value_usd: Optional[float] = None
    active_watts: Optional[float] = None
    temperature_state: str = "GREEN"
    charging: Optional[bool] = True
    available_cpu_pct: Optional[float] = None
    available_ram_mb: Optional[float] = None
    available_bandwidth_mbps: Optional[float] = None


@dataclass
class RevenueEstimate:
    gross_usd: float
    adjusted_gross_usd: float
    costs: dict[str, float]
    total_cost_usd: float
    expected_net_profit_usd: float
    runtime_seconds: float
    failure_probability: float
    volatility_haircut: float
    liquidity_haircut: float
    asset_symbol: str

    @property
    def runtime_hours(self) -> float:
        return self.runtime_seconds / 3600.0

    @property
    def profitable(self) -> bool:
        return self.expected_net_profit_usd > 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["runtime_hours"] = self.runtime_hours
        return d


def requirements_for(terms: OpportunityTerms, constraints: dict[str, Any] | None = None) -> dict[str, float]:
    base = dict(WORKLOAD_DEFAULT_REQUIREMENTS["heavy" if terms.workload_type in HEAVY else "light"])
    base.update({k: float(v) for k, v in (terms.requirements or {}).items() if k in base and v is not None})
    base.update({k: float(v) for k, v in (constraints or {}).items() if k in base and v is not None})
    return base


def estimate_runtime_seconds(terms: OpportunityTerms, device: DeviceProfile, job_max_runtime: Optional[int] = None) -> float:
    base = float(terms.expected_runtime_seconds or WORKLOAD_DEFAULT_RUNTIME_S.get(terms.workload_type, 30))
    if job_max_runtime:
        base = min(base, float(job_max_runtime))
    if terms.workload_type in HEAVY and device.farm_score:
        base *= clamp(50.0 / max(float(device.farm_score), 1.0), 0.5, 3.0)
    return max(base, 0.1)


def haircuts(asset: str, p: CostPolicy) -> tuple[float, float]:
    a = (asset or "USD").upper()
    if a in ("USD", "USDC"):
        return p.volatility_haircut.get(a, 0.0), p.liquidity_haircut.get(a, 0.0)
    if a not in p.prices:
        return p.unknown_asset_haircut, p.unknown_asset_haircut
    return p.volatility_haircut.get(a, p.unknown_asset_haircut), p.liquidity_haircut.get(a, p.unknown_asset_haircut)


def gross_usd(terms: OpportunityTerms, runtime_s: float, p: CostPolicy, override: Optional[float] = None) -> float:
    if override is not None:
        return max(float(override), 0.0)
    if terms.reward_asset_per_job is not None:
        price = p.prices.get((terms.asset_symbol or "USD").upper(), 0.0)
        return max(float(terms.reward_asset_per_job) * price, 0.0)
    if terms.revenue_usd_per_job is not None:
        return max(float(terms.revenue_usd_per_job), 0.0)
    if terms.estimated_revenue_usd_hour is not None:
        return max(float(terms.estimated_revenue_usd_hour) * runtime_s / 3600.0, 0.0)
    return 0.0


def device_watts(device: DeviceProfile, thermal_intensity: float, p: CostPolicy) -> float:
    base = float(device.active_watts) if device.active_watts else p.active_watts
    return base * (0.5 + clamp(thermal_intensity, 0.0, 1.0))


def failure_probability(device: DeviceProfile, terms: OpportunityTerms, p: CostPolicy, retry_count: int = 0) -> float:
    rep = clamp(float(device.reputation), 0.0, 100.0)
    fp = (p.base_failure_probability + 0.2 * (1 - rep / 100.0) + 0.05 * max(retry_count, 0)
          + 0.1 * max(float(terms.thermal_intensity) - 0.5, 0.0))
    return clamp(fp, 0.0, 0.95)


def cost_breakdown(*, runtime_s: float, gross: float, network_bytes: float, device: DeviceProfile,
                   terms: OpportunityTerms, p: CostPolicy, fail_p: float) -> dict[str, float]:
    hours = runtime_s / 3600.0
    purchase = float(device.purchase_price_usd if device.purchase_price_usd is not None else p.default_purchase_usd)
    resale = float(device.resale_value_usd if device.resale_value_usd is not None else p.default_resale_usd)
    fee_pct = p.payout_fee_pct if terms.payout_fee_pct is None else float(terms.payout_fee_pct)
    return {
        "electricity": device_watts(device, float(terms.thermal_intensity), p) * hours / 1000.0 * p.power_price_kwh,
        "network": float(network_bytes) / 1e9 * p.network_cost_per_gb,
        "api": float(terms.estimated_api_cost_usd or 0.0),
        "chain_fee": float(terms.chain_fee_usd_per_job or 0.0),
        "payout_fee": gross * fee_pct,
        "infrastructure": p.infra_cost_per_job + p.infra_cost_per_hour * hours,
        "depreciation": max(purchase - resale, 0.0) / max(p.lifetime_hours, 1.0) * hours
                        * (1.0 + clamp(float(terms.thermal_intensity), 0.0, 1.0)),
        "failure": fail_p * gross,
    }


def estimate_job(terms: OpportunityTerms, device: DeviceProfile, p: CostPolicy, *,
                 gross_override: Optional[float] = None, job_max_runtime: Optional[int] = None,
                 retry_count: int = 0) -> RevenueEstimate:
    runtime_s = estimate_runtime_seconds(terms, device, job_max_runtime)
    gross = gross_usd(terms, runtime_s, p, gross_override)
    vol, liq = haircuts(terms.asset_symbol, p)
    if gross_override is not None:
        vol, liq = 0.0, 0.0  # customer-quoted USD price
    adjusted = gross * (1 - vol) * (1 - liq)
    fp = failure_probability(device, terms, p, retry_count)
    costs = cost_breakdown(runtime_s=runtime_s, gross=gross, network_bytes=terms.expected_network_bytes,
                           device=device, terms=terms, p=p, fail_p=fp)
    total = sum(costs.values())
    return RevenueEstimate(gross_usd=gross, adjusted_gross_usd=adjusted, costs=costs, total_cost_usd=total,
                           expected_net_profit_usd=adjusted - total, runtime_seconds=runtime_s,
                           failure_probability=fp, volatility_haircut=vol, liquidity_haircut=liq,
                           asset_symbol=(terms.asset_symbol or "USD").upper())


def score_opportunity(est: RevenueEstimate, terms: OpportunityTerms, device: DeviceProfile) -> float:
    """Spec §64 score_opportunity."""
    if est.expected_net_profit_usd <= 0:
        return 0.0
    profit_per_hour = est.expected_net_profit_usd / max(est.runtime_hours, 0.01)
    payment_confidence = clamp(float(terms.payment_confidence), 0, 1)
    repeatability = clamp(float(terms.repeatability_score), 0, 1)
    reliability = clamp(float(device.reputation) / 100.0, 0, 1)
    time_to_payment_penalty = 1 / (1 + float(terms.expected_payment_days) / 7)
    risk_penalty = 1 - clamp(float(terms.risk_score), 0, 1)
    intensity = max(float(terms.normalized_resource_consumption), 0.1)
    score = (profit_per_hour * payment_confidence * repeatability * reliability
             * time_to_payment_penalty * risk_penalty / intensity)
    return max(score, 0.0)


def device_fit(device: DeviceProfile, req: dict[str, float]) -> float:
    """Spec §63 calculate_device_fit. Unknown telemetry counts as a neutral 0.8."""
    def ratio(avail: Optional[float], need: float) -> float:
        if avail is None:
            return 0.8
        return clamp(float(avail) / max(need, 0.1), 0.0, 1.0)

    cpu_fit = ratio(device.available_cpu_pct, req["cpu_pct"])
    ram_fit = ratio(device.available_ram_mb, req["ram_mb"])
    net_fit = ratio(device.available_bandwidth_mbps, max(req["bandwidth_mbps"], 0.1))
    thermal = THERMAL_MULTIPLIER.get((device.temperature_state or "UNKNOWN").upper(), 0.6)
    charging = 1.0 if device.charging else 0.75
    return (0.35 * cpu_fit + 0.25 * ram_fit + 0.20 * net_fit + 0.20 * thermal) * charging


def urgency_multiplier(now: datetime, deadline: Optional[datetime], base: float = 1.0) -> float:
    """Spec §147. Never overrides safety (applied after hard exclusions)."""
    if deadline is None:
        return base
    remaining = max((deadline - now).total_seconds(), 0)
    if remaining < 60:
        return 1.25
    if remaining < 300:
        return 1.10
    return 1.0


def final_score(opportunity_score: float, fit: float, reputation: float, urgency: float = 1.0,
                tier: str = "STANDARD") -> float:
    """Spec §146 (thermal and payment confidence are already inside fit / opportunity score)."""
    return (opportunity_score * fit * clamp(reputation / 100.0, 0, 1) * urgency
            * TIER_MULTIPLIER.get((tier or "STANDARD").upper(), 1.0))


def actual_costs(*, runtime_s: float, gross: float, network_bytes: float, device: DeviceProfile,
                 terms: OpportunityTerms, p: CostPolicy) -> dict[str, float]:
    """Spec §65: actuals replace estimates (no failure-probability term: the job succeeded)."""
    c = cost_breakdown(runtime_s=runtime_s, gross=gross, network_bytes=network_bytes, device=device,
                       terms=terms, p=p, fail_p=0.0)
    c.pop("failure", None)
    return c


def device_roi(*, purchase_price: float, net_usd: float, gross_usd: float, runtime_seconds: float,
               days_active: float) -> dict[str, Optional[float]]:
    """Spec §18: hardware recovered %, payback period, realized and projected yields."""
    per_day = net_usd / days_active if days_active > 0 else None
    remaining = max(purchase_price - net_usd, 0.0)
    return {
        "hardware_recovered_pct": round(100.0 * net_usd / purchase_price, 4) if purchase_price > 0 else None,
        "roi_pct": round(100.0 * net_usd / purchase_price, 4) if purchase_price > 0 else None,
        "realized_hourly_yield_usd": (net_usd / (runtime_seconds / 3600.0)) if runtime_seconds > 0 else None,
        "net_per_day_usd": per_day,
        "projected_monthly_yield_usd": per_day * 30 if per_day is not None else None,
        "payback_days_remaining": (remaining / per_day) if per_day and per_day > 0 else None,
        "gross_margin_pct": round(100.0 * net_usd / gross_usd, 4) if gross_usd > 0 else None,
    }
