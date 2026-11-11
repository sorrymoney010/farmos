"""Unit tests: Python agent benchmark runner + telemetry (no network)."""

import hashlib
import json

import pytest

from app.agent import benchmark as b
from app.services.farm_score import score_components


def test_run_benchmark_schema_and_scores(monkeypatch):
    monkeypatch.setattr(b, "_cpu", lambda seconds=1.5: {"single_thread_hashes_per_sec": 1e5,
                                                         "multi_thread_hashes_per_sec": 4e5, "threads": 4})
    monkeypatch.setattr(b, "read_temperature_c", lambda: 30.0)
    raw = b.run_benchmark(None)
    for k in ("cpu", "ai", "memory", "storage", "network", "thermal", "battery", "duration_ms"):
        assert k in raw
    s = score_components(raw)
    assert s.cpu and s.storage is not None and s.anomalies == []
    raw_json, sha = b.encode_results(raw)
    assert hashlib.sha256(raw_json.encode()).hexdigest() == sha
    assert json.loads(raw_json) == json.loads(json.dumps(raw))


def test_benchmark_refuses_when_hot(monkeypatch):
    monkeypatch.setattr(b, "read_temperature_c", lambda: 39.0)
    with pytest.raises(b.BenchmarkAborted):
        b.run_benchmark(None)


def test_telemetry_never_fakes_temperature(monkeypatch):
    monkeypatch.setattr(b, "read_temperature_c", lambda: None)
    monkeypatch.setattr(b, "read_battery", lambda: {})
    t = b.collect_telemetry()
    assert "temperature_c" not in t and "battery_pct" not in t
