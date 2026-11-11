"""Phase 2: pure verification policy + lease arithmetic."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import lease_service as leases
from app.services import reputation_service as rep
from app.services import verification_service as v
from app.workloads import cpu_data


def _job(workload, payload, spec=None):
    return SimpleNamespace(id=uuid.uuid4(), workload_type=workload, requested_resources=payload,
                           verification_spec=spec or {"role": "PRIMARY"})


def _result(obj, *, hash_override=None):
    uri = cpu_data.canonical(obj)
    return SimpleNamespace(result_uri=uri, result_hash=hash_override or cpu_data.sha256_hex(uri))


def test_honest_checksum_passes_recompute():
    p = {"seed": "q", "size_bytes": 2048}
    out = v.evaluate_static(_job("checksum", p), _result(cpu_data.run_checksum(p)))
    assert out.status == v.PASSED and "DETERMINISTIC_RECOMPUTE" in out.method


def test_forged_checksum_fails_even_with_consistent_hash():
    p = {"seed": "q", "size_bytes": 2048}
    forged = dict(cpu_data.run_checksum(p), output_sha256="ab" * 32)
    out = v.evaluate_static(_job("checksum", p), _result(forged))
    assert out.status == v.FAILED and out.penalty == rep.FAIL


def test_hash_mismatch_is_hard_fail_for_deterministic():
    p = {"seed": "q", "size_bytes": 64}
    out = v.evaluate_static(_job("checksum", p), _result(cpu_data.run_checksum(p), hash_override="0" * 64))
    assert out.status == v.FAILED and out.method == "HASH"


def test_hash_mismatch_is_advisory_for_http_check():
    obj = {"status": "COMPLETED", "http_status": 200, "duration_ms": 12}
    out = v.evaluate_static(_job("http_check", {}), _result(obj, hash_override="0" * 64))
    assert out.status == v.PASSED and out.evidence["hash_ok"] is False


def test_legacy_hash_form_accepted():
    obj = {"status": "COMPLETED", "http_status": 200, "duration_ms": 12.0}
    legacy = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    assert v.hash_matches(obj, cpu_data.sha256_hex(legacy))
    assert v.hash_matches(obj, cpu_data.sha256_hex(cpu_data.canonical(obj)))


def test_json_transform_and_compression_verified():
    jp = {"document": [{"v": 1}, {"v": 2}], "ops": [{"op": "sum", "field": "v"}]}
    assert v.evaluate_static(_job("json_transform", jp), _result(cpu_data.run_json_transform(jp))).status == v.PASSED
    bad = dict(cpu_data.run_json_transform(jp), output=4)
    assert v.evaluate_static(_job("json_transform", jp), _result(bad)).status == v.FAILED
    cp = {"seed": "c", "size_bytes": 3000}
    assert v.evaluate_static(_job("compression", cp), _result(cpu_data.run_compression(cp))).status == v.PASSED


def test_device_reported_failure_is_soft():
    out = v.evaluate_static(_job("http_check", {}), _result({"status": "FAILED", "error": "dns"}))
    assert out.status == v.FAILED and out.device_failed and out.penalty == rep.SOFT_FAIL


def test_schema_failure_http():
    out = v.evaluate_static(_job("http_check", {}), _result({"status": "COMPLETED", "http_status": 999}))
    assert out.status == v.FAILED and out.method == "SCHEMA"


def test_canary_known_answer():
    p = {"seed": "k", "size_bytes": 128}
    good = cpu_data.run_checksum(p)
    spec = {"role": "CANARY", "expected": {"output_sha256": good["output_sha256"]}}
    assert v.evaluate_static(_job("checksum", p, spec), _result(good)).status == v.PASSED
    http_spec = {"role": "CANARY", "expected": {"http_status": 204}}
    out = v.evaluate_static(_job("http_check", {}, http_spec), _result({"status": "COMPLETED", "http_status": 200}))
    assert out.status == v.FAILED and out.penalty == rep.CANARY_FAIL


def test_redundant_compare_and_sampling():
    ok, _ = v.compare_redundant("http_check", {"status": "COMPLETED", "http_status": 200},
                                {"status": "COMPLETED", "http_status": 204})
    assert ok
    bad, _ = v.compare_redundant("http_check", {"status": "COMPLETED", "http_status": 200},
                                 {"status": "COMPLETED", "http_status": 503})
    assert not bad
    ids = [uuid.uuid4() for _ in range(2000)]
    rate = sum(v.sampled_for_redundancy(i, 0.2) for i in ids) / len(ids)
    assert 0.15 < rate < 0.25
    assert all(v.sampled_for_redundancy(i, 1.0) for i in ids[:10])
    assert not any(v.sampled_for_redundancy(i, 0.0) for i in ids[:10])
    i = ids[0]
    assert v.sampled_for_redundancy(i, 0.2) == v.sampled_for_redundancy(i, 0.2)  # deterministic


def _lease(**kw):
    now = datetime.now(timezone.utc)
    base = dict(id=uuid.uuid4(), status="ACTIVE", issued_at=now, accepted_at=now,
                expires_at=now + timedelta(seconds=60), grace_seconds=30, renewals=0, attempt=1, outcome=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_lease_liveness_and_grace():
    now = datetime.now(timezone.utc)
    assert leases.is_live(_lease(expires_at=now - timedelta(seconds=10)), now)       # inside grace
    assert not leases.is_live(_lease(expires_at=now - timedelta(seconds=31)), now)
    assert not leases.is_live(_lease(status="EXPIRED"), now)


def test_lease_renew_extends_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "LEASE_RENEW_SECONDS", 120)
    monkeypatch.setattr(settings, "LEASE_MAX_TOTAL_SECONDS", 300)
    now = datetime.now(timezone.utc)
    l = _lease(accepted_at=now - timedelta(seconds=100), expires_at=now + timedelta(seconds=10))
    leases.renew(l, now)
    assert l.renewals == 1 and l.expires_at == now + timedelta(seconds=120)
    l2 = _lease(accepted_at=now - timedelta(seconds=250), expires_at=now + timedelta(seconds=10))
    leases.renew(l2, now)
    assert l2.expires_at == l2.accepted_at + timedelta(seconds=300)  # capped
    l3 = _lease(accepted_at=now - timedelta(seconds=400), expires_at=now + timedelta(seconds=5))
    with pytest.raises(leases.LeaseError):
        leases.renew(l3, now)
    with pytest.raises(leases.LeaseError):
        leases.renew(_lease(expires_at=now - timedelta(seconds=100)), now)
