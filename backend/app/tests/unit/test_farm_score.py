"""Unit tests: FarmScore scoring (spec §9) — pure functions, no DB."""

import pytest

from app.services import farm_score as fs

GOOD_RAW = {
    "cpu": {"single_thread_hashes_per_sec": 250_000, "multi_thread_hashes_per_sec": 1_500_000, "threads": 8},
    "ai": {"matmul_gflops": 5.0},
    "memory": {"copy_mb_per_sec": 8000, "total_mb": 8192, "available_mb": 4000},
    "storage": {"write_mb_per_sec": 200, "read_mb_per_sec": 500, "free_mb": 40000},
    "network": {"latency_ms": 15, "down_mbps": 100, "up_mbps": 20},
    "thermal": {"start_c": 30.0, "end_c": 31.0, "peak_c": 31.5},
    "battery": {"pct": 90, "charging": True, "health": "GOOD", "temperature_c": 30},
}


def test_reference_device_scores_100_everywhere():
    s = fs.score_components(GOOD_RAW, reliability=100.0)
    for k, v in s.as_dict().items():
        assert v == pytest.approx(100.0), k
    assert s.anomalies == []
    assert fs.calculate_farm_score(s.as_dict()) == pytest.approx(100.0)


def test_spec_example_farmscore_formula():
    # Spec §9 example components with memory = 0 (not listed) -> weighted sum.
    scores = {"cpu": 78, "ai": 61, "memory": 0, "storage": 70, "network": 93,
              "thermal": 74, "power_efficiency": 82, "reliability": 98}
    expected = 0.2 * 78 + 0.15 * 61 + 0.1 * 70 + 0.15 * 93 + 0.1 * 74 + 0.1 * 82 + 0.1 * 98
    assert fs.calculate_farm_score(scores) == pytest.approx(expected, abs=1e-3)


def test_weights_are_normalised_and_validated():
    w = fs.normalise_weights({"cpu": 2, "ai": 2})
    assert w["cpu"] == pytest.approx(0.5) and w["memory"] == 0
    assert fs.calculate_farm_score({"cpu": 80, "ai": 40}, {"cpu": 1, "ai": 1}) == pytest.approx(60)
    with pytest.raises(ValueError):
        fs.normalise_weights({"cpu": -1, "ai": 2})
    with pytest.raises(ValueError):
        fs.normalise_weights({"cpu": 0})


def test_missing_components_score_zero_not_inflated():
    only_cpu = fs.score_components({"cpu": GOOD_RAW["cpu"]})
    assert only_cpu.ai is None
    assert fs.calculate_farm_score(only_cpu.as_dict()) == pytest.approx(20.0)


def test_hot_benchmark_reduces_thermal_score():
    raw = dict(GOOD_RAW, thermal={"start_c": 30, "end_c": 41, "peak_c": 42})
    s = fs.score_components(raw, green_max_c=38)
    # rise 12 -> -(10*5)=-50 ; over green 4 -> -32 => 18
    assert s.thermal == pytest.approx(18.0)


def test_implausible_values_flagged():
    raw = dict(GOOD_RAW, cpu={"multi_thread_hashes_per_sec": 9e9})
    s = fs.score_components(raw)
    assert s.anomalies and "exceeds plausible max" in s.anomalies[0]
    assert s.cpu <= 100


def test_bad_battery_and_discharging_power_efficiency():
    raw = dict(GOOD_RAW, battery={"charging": False, "health": "GOOD"})
    assert fs.score_components(raw).power_efficiency == pytest.approx(80.0)
    raw = dict(GOOD_RAW, battery={"charging": True, "health": "DEAD"})
    assert fs.score_components(raw).power_efficiency == 0


def test_reliability_from_heartbeats():
    assert fs.reliability_from_heartbeats(0, 60) == 50.0  # new node: neutral
    assert fs.reliability_from_heartbeats(2880, 86_400 * 3) == pytest.approx(100.0)
    assert fs.reliability_from_heartbeats(1440, 86_400) == pytest.approx(50.0)


def test_garbage_values_ignored():
    raw = {"cpu": {"multi_thread_hashes_per_sec": "nan"}, "ai": {"matmul_gflops": -5},
           "memory": {"copy_mb_per_sec": True}}
    s = fs.score_components(raw)
    assert s.cpu is None and s.ai is None and s.memory is None
