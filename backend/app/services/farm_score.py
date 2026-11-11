"""FarmScore computation for benchmark suite ``farmos-bench-1`` (spec §9, §57.2).

Design rules
------------
* Devices submit RAW measurements; the server converts them to 0–100 component
  scores. Devices never self-report scores, which limits benchmark spoofing
  (spec §37 "spoofed benchmarks").
* Every score references a suite version and the exact weights used, so a
  FarmScore is reproducible from ``raw_results`` alone.
* Weights are configurable (settings / admin system_settings) and are
  normalised by their sum, so admins don't have to make them add up to 1.
* Missing components score 0 (conservative: never inflate an unmeasured node).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

SUITE_VERSION = "farmos-bench-1"

COMPONENTS = (
    "cpu",
    "ai",
    "memory",
    "storage",
    "network",
    "thermal",
    "power_efficiency",
    "reliability",
)

DEFAULT_WEIGHTS: dict[str, float] = {
    "cpu": 0.20,
    "ai": 0.15,
    "memory": 0.10,
    "storage": 0.10,
    "network": 0.15,
    "thermal": 0.10,
    "power_efficiency": 0.10,
    "reliability": 0.10,
}

# Reference values that earn 100 points (a strong 2021-era flagship phone).
REFERENCE = {
    "cpu_multi_hashes_per_sec": 1_500_000.0,  # SHA-256 over 1 KiB blocks, all threads
    "cpu_single_hashes_per_sec": 250_000.0,
    "ai_matmul_gflops": 5.0,  # naive fp32 matmul, JVM/Python-level
    "memory_copy_mb_per_sec": 8_000.0,
    "memory_total_mb": 8_192.0,
    "storage_write_mb_per_sec": 200.0,
    "storage_read_mb_per_sec": 500.0,
    "network_down_mbps": 100.0,
    "network_up_mbps": 20.0,
}

# Values beyond these are physically implausible for a phone-class node and are
# flagged as anomalies (benchmark does not pass baseline; health event raised).
PLAUSIBLE_MAX = {
    ("cpu", "multi_thread_hashes_per_sec"): 50_000_000.0,
    ("cpu", "single_thread_hashes_per_sec"): 10_000_000.0,
    ("ai", "matmul_gflops"): 2_000.0,
    ("memory", "copy_mb_per_sec"): 200_000.0,
    ("storage", "write_mb_per_sec"): 10_000.0,
    ("storage", "read_mb_per_sec"): 20_000.0,
    ("network", "down_mbps"): 20_000.0,
    ("network", "up_mbps"): 20_000.0,
}

BATTERY_HEALTH_FACTOR = {
    "GOOD": 1.0,
    "COLD": 0.8,
    "UNKNOWN": 0.7,
    "OVERHEAT": 0.3,
    "DEAD": 0.0,
    "OVER_VOLTAGE": 0.0,
    "UNSPECIFIED_FAILURE": 0.0,
}


@dataclass
class ComponentScores:
    cpu: float | None = None
    ai: float | None = None
    memory: float | None = None
    storage: float | None = None
    network: float | None = None
    thermal: float | None = None
    power_efficiency: float | None = None
    reliability: float | None = None
    anomalies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, float | None]:
        d = asdict(self)
        d.pop("anomalies")
        return d


def _num(section: dict[str, Any] | None, key: str) -> float | None:
    if not isinstance(section, dict):
        return None
    v = section.get(key)
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f < 0:
        return None
    return f


def _ratio(value: float | None, ref: float) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, 100.0 * value / ref))


def _clamp(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 4)


def _blend(parts: list[tuple[float | None, float]]) -> float | None:
    present = [(v, w) for v, w in parts if v is not None]
    if not present:
        return None
    total_w = sum(w for _, w in present)
    return _clamp(sum(v * w for v, w in present) / total_w)


def detect_anomalies(raw: dict[str, Any]) -> list[str]:
    out = []
    for (section, key), limit in PLAUSIBLE_MAX.items():
        v = _num(raw.get(section), key)
        if v is not None and v > limit:
            out.append(f"{section}.{key}={v:g} exceeds plausible max {limit:g}")
    th = raw.get("thermal") or {}
    start, peak = _num(th, "start_c"), _num(th, "peak_c")
    if start is not None and peak is not None and peak < start - 0.5:
        out.append("thermal.peak_c below thermal.start_c")
    return out


def score_components(
    raw: dict[str, Any],
    *,
    green_max_c: float = 38.0,
    reliability: float | None = None,
) -> ComponentScores:
    """Convert raw ``farmos-bench-1`` measurements into 0–100 component scores."""
    cpu = raw.get("cpu") or {}
    ai = raw.get("ai") or {}
    mem = raw.get("memory") or {}
    sto = raw.get("storage") or {}
    net = raw.get("network") or {}
    th = raw.get("thermal") or {}
    bat = raw.get("battery") or {}

    s = ComponentScores(anomalies=detect_anomalies(raw))

    s.cpu = _blend(
        [
            (_ratio(_num(cpu, "multi_thread_hashes_per_sec"), REFERENCE["cpu_multi_hashes_per_sec"]), 0.7),
            (_ratio(_num(cpu, "single_thread_hashes_per_sec"), REFERENCE["cpu_single_hashes_per_sec"]), 0.3),
        ]
    )
    s.ai = _blend([(_ratio(_num(ai, "matmul_gflops"), REFERENCE["ai_matmul_gflops"]), 1.0)])
    s.memory = _blend(
        [
            (_ratio(_num(mem, "copy_mb_per_sec"), REFERENCE["memory_copy_mb_per_sec"]), 0.7),
            (_ratio(_num(mem, "total_mb"), REFERENCE["memory_total_mb"]), 0.3),
        ]
    )
    storage = _blend(
        [
            (_ratio(_num(sto, "write_mb_per_sec"), REFERENCE["storage_write_mb_per_sec"]), 0.5),
            (_ratio(_num(sto, "read_mb_per_sec"), REFERENCE["storage_read_mb_per_sec"]), 0.5),
        ]
    )
    free_mb = _num(sto, "free_mb")
    if storage is not None and free_mb is not None and free_mb < 2_000:
        storage = _clamp(storage * 0.5)  # nearly-full storage is a poor host
    s.storage = storage

    latency = _num(net, "latency_ms")
    latency_score = None
    if latency is not None:
        # 100 at <=20 ms, 0 at >=500 ms, linear between.
        latency_score = _clamp(100.0 * (500.0 - max(20.0, latency)) / 480.0)
    s.network = _blend(
        [
            (latency_score, 0.5),
            (_ratio(_num(net, "down_mbps"), REFERENCE["network_down_mbps"]), 0.35),
            (_ratio(_num(net, "up_mbps"), REFERENCE["network_up_mbps"]), 0.15),
        ]
    )

    start, peak = _num(th, "start_c"), _num(th, "peak_c")
    if peak is not None:
        rise = max(0.0, peak - (start if start is not None else peak))
        thermal = 100.0 - max(0.0, rise - 2.0) * 5.0 - max(0.0, peak - green_max_c) * 8.0
        s.thermal = _clamp(thermal)

    health = str(bat.get("health") or "UNKNOWN").upper()
    factor = BATTERY_HEALTH_FACTOR.get(health, 0.7)
    charging = bat.get("charging")
    # Proxy for power efficiency until per-job energy metering exists: battery
    # health dominates; running off-charger costs battery cycles (spec §63 x0.75).
    if bat:
        s.power_efficiency = _clamp(100.0 * factor * (1.0 if charging else 0.8))

    s.reliability = reliability
    return s


def normalise_weights(weights: dict[str, float] | None) -> dict[str, float]:
    w = {k: float(v) for k, v in (weights or DEFAULT_WEIGHTS).items() if k in COMPONENTS}
    for k in COMPONENTS:
        w.setdefault(k, 0.0)
    if any(v < 0 for v in w.values()):
        raise ValueError("FarmScore weights must be non-negative")
    total = sum(w.values())
    if total <= 0:
        raise ValueError("FarmScore weights must sum to > 0")
    return {k: round(v / total, 6) for k, v in w.items()}


def calculate_farm_score(scores: dict[str, float | None], weights: dict[str, float] | None = None) -> float:
    """Weighted FarmScore (0–100). Missing components count as 0."""
    w = normalise_weights(weights)
    total = sum(w[k] * float(scores.get(k) or 0.0) for k in COMPONENTS)
    return _clamp(total)


def reliability_from_heartbeats(heartbeats_24h: int, seconds_since_enroll: float, interval_s: int = 30) -> float:
    """Uptime-based reliability: received / expected heartbeats over the last 24h.

    A brand-new node (no meaningful history yet) gets a neutral 50.
    """
    window = min(86_400.0, max(0.0, seconds_since_enroll))
    if window < 600:  # < 10 min of history
        return 50.0
    expected = window / interval_s
    return _clamp(100.0 * heartbeats_24h / expected)
