"""Python agent benchmark runner + host telemetry (suite farmos-bench-1).

Mirrors the Android BenchmarkRunner so simulator/Linux nodes produce the same
raw-results schema. Measurements are deliberately short (a few seconds) and the
runner refuses to start when the host is not thermally GREEN (protect hardware).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

SUITE_VERSION = "farmos-bench-1"
GREEN_MAX_C = 38.0
ABORT_C = 45.0


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_temperature_c() -> float | None:
    """Hottest thermal zone (Linux sysfs, millidegrees), or None if unavailable."""
    temps = []
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        v = _read(p)
        if v and v.lstrip("-").isdigit():
            c = int(v) / 1000.0
            if 0 < c < 130:
                temps.append(c)
    return round(max(temps), 1) if temps else None


def read_battery() -> dict[str, Any]:
    """Battery pct/charging/health from /sys/class/power_supply, or {} on mains-only hosts."""
    for base in glob.glob("/sys/class/power_supply/*"):
        if _read(os.path.join(base, "type")) != "Battery":
            continue
        cap = _read(os.path.join(base, "capacity"))
        status = (_read(os.path.join(base, "status")) or "").lower()
        health = (_read(os.path.join(base, "health")) or "GOOD").upper().replace(" ", "_")
        return {
            "battery_pct": float(cap) if cap and cap.isdigit() else None,
            "charging": status in {"charging", "full"},
            "battery_health": health,
        }
    return {}


def collect_telemetry() -> dict[str, Any]:
    """Real heartbeat telemetry. Unknown values are omitted, never faked."""
    t: dict[str, Any] = {}
    temp = read_temperature_c()
    if temp is not None:
        t["temperature_c"] = temp
    bat = read_battery()
    t.update({k: v for k, v in bat.items() if v is not None})
    if not bat:
        t["charging"] = True  # mains-powered host (no battery present)
    try:
        t["storage_free_mb"] = int(shutil.disk_usage(tempfile.gettempdir()).free / 1_048_576)
    except OSError:
        pass
    return t


def _hash_worker(seconds: float) -> int:
    block = os.urandom(1024)
    n, end = 0, time.perf_counter() + seconds
    while time.perf_counter() < end:
        for _ in range(200):
            hashlib.sha256(block).digest()
        n += 200
    return n


def _cpu(seconds: float = 1.5) -> dict[str, Any]:
    single = _hash_worker(seconds) / seconds
    threads = max(1, os.cpu_count() or 1)
    try:
        with ProcessPoolExecutor(max_workers=threads) as ex:
            multi = sum(ex.map(_hash_worker, [seconds] * threads)) / seconds
    except (OSError, RuntimeError):
        multi = single
    return {"single_thread_hashes_per_sec": round(single, 1),
            "multi_thread_hashes_per_sec": round(multi, 1), "threads": threads}


def _ai(n: int = 96) -> dict[str, Any]:
    import random as _r

    a = [[_r.random() for _ in range(n)] for _ in range(n)]
    b = [[_r.random() for _ in range(n)] for _ in range(n)]
    bt = list(zip(*b))
    start = time.perf_counter()
    _ = [[sum(x * y for x, y in zip(row, col)) for col in bt] for row in a]
    secs = max(1e-6, time.perf_counter() - start)
    return {"matmul_gflops": round(2 * n**3 / secs / 1e9, 4), "n": n}


def _memory() -> dict[str, Any]:
    buf = bytearray(32 * 1_048_576)
    start = time.perf_counter()
    for _ in range(8):
        bytes(buf)
    secs = max(1e-6, time.perf_counter() - start)
    info: dict[str, Any] = {"copy_mb_per_sec": round(8 * 32 / secs, 1)}
    meminfo = _read("/proc/meminfo") or ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            info["total_mb"] = int(line.split()[1]) // 1024
        elif line.startswith("MemAvailable:"):
            info["available_mb"] = int(line.split()[1]) // 1024
    return info


def _storage(size_mb: int = 32) -> dict[str, Any]:
    data = os.urandom(1_048_576)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        start = time.perf_counter()
        with open(path, "wb") as f:
            for _ in range(size_mb):
                f.write(data)
            f.flush()
            os.fsync(f.fileno())
        w = size_mb / max(1e-6, time.perf_counter() - start)
        start = time.perf_counter()
        with open(path, "rb") as f:
            while f.read(1_048_576):
                pass
        r = size_mb / max(1e-6, time.perf_counter() - start)
    finally:
        os.unlink(path)
    free = int(shutil.disk_usage(tempfile.gettempdir()).free / 1_048_576)
    return {"write_mb_per_sec": round(w, 1), "read_mb_per_sec": round(r, 1), "free_mb": free}


def _network(api_base: str | None) -> dict[str, Any]:
    if not api_base:
        return {"latency_ms": None}
    import httpx

    samples = []
    for _ in range(3):
        start = time.perf_counter()
        try:
            httpx.get(f"{api_base.rstrip('/')}/health", timeout=5)
            samples.append((time.perf_counter() - start) * 1000)
        except httpx.HTTPError:
            pass
    return {"latency_ms": round(min(samples), 1) if samples else None}


class BenchmarkAborted(RuntimeError):
    pass


def run_benchmark(api_base: str | None = None) -> dict[str, Any]:
    """Run suite farmos-bench-1 and return the raw-results dict."""
    start_c = read_temperature_c()
    if start_c is not None and start_c >= GREEN_MAX_C:
        raise BenchmarkAborted(f"host at {start_c}C; benchmark needs GREEN (<{GREEN_MAX_C}C)")
    t0 = time.perf_counter()
    peak = start_c
    results: dict[str, Any] = {}
    for name, fn in (("cpu", _cpu), ("ai", _ai), ("memory", _memory), ("storage", _storage)):
        results[name] = fn()
        now_c = read_temperature_c()
        if now_c is not None:
            peak = max(peak or now_c, now_c)
            if now_c > ABORT_C:
                raise BenchmarkAborted(f"aborted: {now_c}C during {name}")
    results["network"] = _network(api_base)
    end_c = read_temperature_c()
    bat = read_battery()
    results["thermal"] = {"start_c": start_c, "end_c": end_c,
                          "peak_c": max(x for x in (peak, end_c) if x is not None) if (peak or end_c) else None}
    results["battery"] = {"pct": bat.get("battery_pct"), "charging": bat.get("charging", True),
                          "health": bat.get("battery_health", "GOOD" if not bat else "UNKNOWN")}
    results["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    return results


def encode_results(raw: dict[str, Any]) -> tuple[str, str]:
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return raw_json, hashlib.sha256(raw_json.encode()).hexdigest()
