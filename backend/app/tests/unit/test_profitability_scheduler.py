"""Pure tests: profitability formulas (spec §63/§64/§146), and deterministic
scheduler ranking over 200 simulated devices (spec §81 DoD: 100 fake nodes)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.config import settings
from app.db.models.enums import DeviceStatus
from app.services import profitability as prof
from app.services import scheduler_service as sched

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
POLICY = prof.CostPolicy.from_settings(settings, {})


def test_internal_http_check_is_profitable_with_cost_breakdown():
    t = prof.OpportunityTerms(workload_type="http_check", revenue_usd_per_job=0.0025, expected_runtime_seconds=5)
    est = prof.estimate_job(t, prof.DeviceProfile(), POLICY)
    assert est.profitable and est.expected_net_profit_usd > 0
    for k in ("electricity", "network", "infrastructure", "payout_fee", "depreciation", "failure", "api", "chain_fee"):
        assert k in est.costs
    assert est.total_cost_usd == pytest.approx(sum(est.costs.values()))
    assert est.expected_net_profit_usd == pytest.approx(est.adjusted_gross_usd - est.total_cost_usd)


def test_token_rewards_get_volatility_and_liquidity_haircuts():
    t = prof.OpportunityTerms(adapter_id="acurast_sim", workload_type="checksum", asset_symbol="ACU",
                              reward_asset_per_job=0.25, expected_runtime_seconds=60)
    est = prof.estimate_job(t, prof.DeviceProfile(), POLICY)
    assert est.gross_usd == pytest.approx(0.25 * 0.02)
    assert est.adjusted_gross_usd == pytest.approx(est.gross_usd * (1 - 0.35) * (1 - 0.20))
    unknown = prof.estimate_job(prof.OpportunityTerms(asset_symbol="ZZZ", reward_asset_per_job=1.0), prof.DeviceProfile(), POLICY)
    assert unknown.gross_usd == 0  # unknown price -> never assumed valuable


def test_unprofitable_work_is_flagged():
    t = prof.OpportunityTerms(adapter_id="acurast_sim", workload_type="checksum", asset_symbol="ACU",
                              reward_asset_per_job=0.01, expected_runtime_seconds=300, thermal_intensity=0.9)
    est = prof.estimate_job(t, prof.DeviceProfile(), POLICY)
    assert not est.profitable and est.expected_net_profit_usd < 0


def test_policy_power_price_changes_profitability():
    t = prof.OpportunityTerms(workload_type="http_check", revenue_usd_per_job=0.0025, expected_runtime_seconds=5)
    cheap = prof.estimate_job(t, prof.DeviceProfile(), POLICY)
    pricey = prof.estimate_job(t, prof.DeviceProfile(), prof.CostPolicy.from_settings(settings, {"power_price_kwh": 500}))
    assert pricey.costs["electricity"] > cheap.costs["electricity"]
    assert pricey.expected_net_profit_usd < cheap.expected_net_profit_usd


def test_device_fit_spec_63_weights_thermal_and_charging():
    req = {"cpu_pct": 10, "ram_mb": 64, "bandwidth_mbps": 1}
    full = prof.DeviceProfile(available_cpu_pct=100, available_ram_mb=4096, available_bandwidth_mbps=50)
    assert prof.device_fit(full, req) == pytest.approx(1.0)
    yellow = prof.DeviceProfile(available_cpu_pct=100, available_ram_mb=4096, available_bandwidth_mbps=50,
                                temperature_state="YELLOW")
    assert prof.device_fit(yellow, req) == pytest.approx(0.35 + 0.25 + 0.20 + 0.20 * 0.8)
    unplugged = prof.DeviceProfile(available_cpu_pct=100, available_ram_mb=4096, available_bandwidth_mbps=50, charging=False)
    assert prof.device_fit(unplugged, req) == pytest.approx(0.75)
    red = prof.DeviceProfile(temperature_state="RED", available_cpu_pct=100, available_ram_mb=4096, available_bandwidth_mbps=50)
    assert prof.device_fit(red, req) == pytest.approx(0.80)  # thermal term zero (and RED is hard-excluded anyway)


def test_urgency_and_tier_multipliers():
    assert prof.urgency_multiplier(NOW, NOW + timedelta(seconds=30)) == pytest.approx(1.25)
    assert prof.urgency_multiplier(NOW, NOW + timedelta(seconds=200)) == pytest.approx(1.10)
    assert prof.urgency_multiplier(NOW, None) == pytest.approx(1.0)
    base = prof.final_score(1.0, 1.0, 100.0)
    assert prof.final_score(1.0, 1.0, 100.0, tier="ENTERPRISE") == pytest.approx(base * 1.2)
    assert prof.final_score(1.0, 1.0, 50.0) == pytest.approx(base * 0.5)


def test_slower_device_has_lower_score():
    t = prof.OpportunityTerms(workload_type="checksum", revenue_usd_per_job=0.002, expected_runtime_seconds=30)
    fast = prof.DeviceProfile(farm_score=90)
    slow = prof.DeviceProfile(farm_score=15)
    ef, es = prof.estimate_job(t, fast, POLICY), prof.estimate_job(t, slow, POLICY)
    assert es.runtime_seconds > ef.runtime_seconds
    assert prof.score_opportunity(ef, t, fast) > prof.score_opportunity(es, t, slow)


# ── scheduler ranking (pure) ─────────────────────────────────────────────────

def _fake_device(i: int, rng: random.Random, **over):
    d = SimpleNamespace(
        id=UUID(int=i + 1), human_id=f"DEV-{i:04d}", provider_id=UUID(int=10_000 + i % 7),
        status=DeviceStatus.IDLE, last_seen_at=NOW - timedelta(seconds=rng.randint(0, 120)),
        temperature_state=rng.choice(["GREEN", "GREEN", "GREEN", "YELLOW", "ORANGE", "RED", "COOLING"]),
        farm_score=rng.uniform(5, 95), reputation_score=rng.uniform(10, 100), ram_mb=rng.choice([2048, 4096, 8192]),
    )
    for k, v in over.items():
        setattr(d, k, v)
    return d


def _cands(n: int, seed: int = 7):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        d = _fake_device(i, rng)
        prof_ = prof.DeviceProfile(device_id=str(d.id), farm_score=d.farm_score, reputation=d.reputation_score,
                                   temperature_state=d.temperature_state, charging=rng.random() > 0.2,
                                   available_cpu_pct=rng.uniform(5, 100), available_ram_mb=rng.uniform(100, 4000),
                                   available_bandwidth_mbps=rng.uniform(0.2, 50))
        out.append(sched.Candidate(device=d, profile=prof_, busy=1 if rng.random() < 0.1 else 0))
    return out


def _ctx(**kw):
    t = prof.OpportunityTerms(workload_type="checksum", revenue_usd_per_job=0.002, expected_runtime_seconds=30)
    return sched.JobContext(job_id="j", workload_type="checksum", priority=100, created_at=NOW, terms=t, **kw)


def test_ranking_200_devices_is_deterministic_and_respects_hard_exclusions():
    cands = _cands(200)
    caps = sched.Caps()
    r1, ex1 = sched.rank(_ctx(), cands, POLICY, caps, NOW)
    r2, ex2 = sched.rank(_ctx(), list(reversed(_cands(200))), POLICY, caps, NOW)
    assert [str(r["device"].id) for r in r1] == [str(r["device"].id) for r in r2]
    assert ex1 == ex2
    assert len(r1) + len(ex1) == 200 and r1
    for r in r1:
        assert r["device"].temperature_state not in ("RED", "COOLING")
        assert r["estimate"].expected_net_profit_usd > 0
    assert all(c.busy == 0 for c in cands if str(c.device.id) in {str(r["device"].id) for r in r1})
    scores = [r["score"] for r in r1]
    assert scores == sorted(scores, reverse=True)
    assert any("thermal" in v for v in ex1.values()) and any("busy" in v for v in ex1.values())


def test_exclusions_affinity_failed_device_min_farmscore_offline_and_provider_cap():
    cands = _cands(50, seed=3)
    for c in cands:
        c.device.temperature_state, c.busy = "GREEN", 0
    target = cands[5].device
    r, _ = sched.rank(_ctx(constraints={"device_affinity": [str(target.id)]}), cands, POLICY, sched.Caps(), NOW)
    assert [x["device"].id for x in r] == [target.id]

    r, ex = sched.rank(_ctx(excluded={str(target.id)}), cands, POLICY, sched.Caps(), NOW)
    assert target.id not in {x["device"].id for x in r} and "failed attempt" in ex[str(target.id)]

    r, ex = sched.rank(_ctx(constraints={"min_farm_score": 60}), cands, POLICY, sched.Caps(), NOW)
    assert all(float(x["device"].farm_score) >= 60 for x in r)

    cands[0].device.last_seen_at = NOW - timedelta(hours=1)
    _, ex = sched.rank(_ctx(), cands, POLICY, sched.Caps(), NOW)
    assert ex[str(cands[0].device.id)] == "offline"

    capped = sched.Caps(provider_inflight={str(cands[1].device.provider_id): 1}, max_per_provider=1)
    _, ex = sched.rank(_ctx(), cands, POLICY, capped, NOW)
    assert ex[str(cands[1].device.id)] == "provider cap reached"


def test_safety_governor_block_reason_is_honoured():
    cands = _cands(5, seed=1)
    for c in cands:
        c.device.temperature_state, c.busy = "GREEN", 0
    cands[0].block_reason = lambda wt: "battery 10% below 20%"
    _, ex = sched.rank(_ctx(), cands, POLICY, sched.Caps(), NOW)
    assert ex[str(cands[0].device.id)].startswith("battery")


def test_higher_reputation_wins_when_otherwise_equal():
    rng = random.Random(0)
    a = _fake_device(1, rng, temperature_state="GREEN", farm_score=50.0, reputation_score=90.0)
    b = _fake_device(2, rng, temperature_state="GREEN", farm_score=50.0, reputation_score=40.0)
    mk = lambda d: sched.Candidate(device=d, profile=prof.DeviceProfile(  # noqa: E731
        device_id=str(d.id), farm_score=50.0, reputation=d.reputation_score, available_cpu_pct=90,
        available_ram_mb=2000, available_bandwidth_mbps=10))
    r, _ = sched.rank(_ctx(), [mk(b), mk(a)], POLICY, sched.Caps(), NOW)
    assert r[0]["device"].id == a.id


def test_effective_priority_ages_old_jobs():
    old = sched.effective_priority(100, NOW - timedelta(minutes=30), NOW)
    new = sched.effective_priority(120, NOW, NOW)
    assert old > new  # anti-starvation
    assert sched.effective_priority(0, NOW - timedelta(days=30), NOW) == pytest.approx(settings.SCHEDULER_AGING_MAX)
