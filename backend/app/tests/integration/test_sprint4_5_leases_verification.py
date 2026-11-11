"""Sprint 4/5 integration: job leases, retries/dead-letter, idempotent results,
independent verification (recompute / redundant / canary), reputation + quarantine,
credit only after verification.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.tests.integration.test_sprint3_benchmarks_safety import (  # noqa: F401 (fixtures)
    BASE, Node, _activate, _now, _sign, admin, client,
)
from app.workloads import cpu_data


def _sql(stmt: str, **params):
    async def _run():
        eng = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with eng.begin() as conn:
                res = await conn.execute(text(stmt), params)
                return res.fetchall() if res.returns_rows else None
        finally:
            await eng.dispose()
    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _no_random_redundancy(monkeypatch):
    monkeypatch.setattr(settings, "REDUNDANT_SAMPLE_RATE", 0.0)


def _isolate(*nodes):
    """Make every other device look offline so redundant shadows land on ours."""
    ids = ",".join(f"'{n.id}'" for n in nodes)
    _sql(f"UPDATE devices SET last_seen_at = now() - interval '2 hours' WHERE id::text NOT IN ({ids})")


def _create(client, admin, workload, payload, headers=None, **kw):
    r = client.post(f"{BASE}/jobs/", headers={**admin, **(headers or {})},
                    json={"workload_type": workload, "payload": payload, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _dispatch(client, admin, job_id, node):
    r = client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job_id, "device_id": node.id})
    assert r.status_code == 200, r.text
    return r.json()


def _accept(node, job_id):
    acc = {"request_nonce": uuid4().hex, "request_timestamp": _now(), "job_id": job_id, "device_id": node.id}
    return node.c.post(f"{BASE}/jobs/device/jobs/{job_id}/accept", headers=node.h,
                       json={"request_nonce": acc["request_nonce"], "request_timestamp": acc["request_timestamp"],
                             "signature": _sign(node.key, acc)})


def _submit(node, job_id, result: dict, *, nonce=None, hash_override=None):
    uri = cpu_data.canonical(result)
    res = {"request_nonce": nonce or uuid4().hex, "request_timestamp": _now(), "job_id": job_id,
           "device_id": node.id, "result_hash": hash_override or cpu_data.sha256_hex(uri), "result_uri": uri}
    body = {k: res[k] for k in ("request_nonce", "request_timestamp", "result_hash", "result_uri")}
    body["signature"] = _sign(node.key, res)
    return node.c.post(f"{BASE}/jobs/device/jobs/{job_id}/result", headers=node.h, json=body)


def _renew(node, job_id, lease_id):
    p = {"request_nonce": uuid4().hex, "request_timestamp": _now(), "job_id": job_id, "device_id": node.id,
         "lease_id": lease_id}
    return node.c.post(f"{BASE}/jobs/device/jobs/{job_id}/lease/renew", headers=node.h,
                       json={"request_nonce": p["request_nonce"], "request_timestamp": p["request_timestamp"],
                             "lease_id": lease_id, "signature": _sign(node.key, p)})


def _poll(node):
    r = node.c.post(f"{BASE}/jobs/device/jobs/next", headers=node.h)
    assert r.status_code == 200, r.text
    return r.json()


def _earnings(client, admin, node):
    return client.get(f"{BASE}/jobs/device/{node.id}/earnings", headers=admin).json()["test_earnings"]["balance_usd"]


def _job(client, admin, job_id):
    return client.get(f"{BASE}/jobs/{job_id}", headers=admin).json()


def _ready_node(client, admin):
    n = Node(client, admin)
    _activate(n)
    return n


CHK = {"seed": "farm", "size_bytes": 65536, "algorithm": "sha256"}


def test_checksum_lease_verify_credit_and_idempotency(client, admin):
    n = _ready_node(client, admin)
    job = _create(client, admin, "checksum", CHK)["job_id"]
    d = _dispatch(client, admin, job, n)
    assert d["lease"]["lease_id"] and d["lease"]["attempt"] == 1

    polled = _poll(n)
    assert polled["job"]["job_id"] == job
    assert polled["lease"]["lease_id"] == d["lease"]["lease_id"]
    assert "verification_spec" not in json.dumps(polled)

    a = _accept(n, job)
    assert a.status_code == 200, a.text
    lease = a.json()["lease"]
    assert lease["accepted_at"] is not None
    again = _accept(n, job)  # idempotent re-accept
    assert again.status_code == 200 and again.json()["idempotent_replay"] is True

    r = _renew(n, job, lease["lease_id"])
    assert r.status_code == 200, r.text
    assert r.json()["lease"]["renewals"] in (0, 1)

    before = _earnings(client, admin, n)
    result = cpu_data.run_checksum(CHK)
    s = _submit(n, job, result)
    assert s.status_code == 200, s.text
    body = s.json()
    assert body["status"] == "VERIFIED"
    assert body["verification"]["verification_status"] == "PASSED"
    assert body["verification"]["test_earnings_credited"] is True
    after = _earnings(client, admin, n)
    assert after == pytest.approx(before + 0.0001)

    # Same result again (network retry) -> replay, no double credit.
    s2 = _submit(n, job, result)
    assert s2.status_code == 200 and s2.json()["idempotent_replay"] is True
    # Different result for the same attempt -> conflict.
    s3 = _submit(n, job, dict(result, output_sha256="00" * 32))
    assert s3.status_code == 409
    # Admin verify is idempotent too.
    v = client.post(f"{BASE}/jobs/verify/result", headers=admin, json={"job_id": job})
    assert v.status_code == 200 and v.json()["is_valid"] is True and v.json()["idempotent_replay"] is True
    assert _earnings(client, admin, n) == pytest.approx(after)

    info = _job(client, admin, job)
    assert info["verification"]["status"] == "PASSED" and info["lease"]["outcome"] == "VERIFIED"
    assert n.get(admin)["status"] == "ACTIVE"


def test_wrong_answer_requeues_penalises_and_quarantines(client, admin):
    n = _ready_node(client, admin)
    rep0 = float(_sql("SELECT reputation_score FROM devices WHERE id = :i", i=n.id)[0][0])
    jobs = []
    for i in range(settings.VERIFICATION_FAILURES_QUARANTINE):
        payload = {"seed": f"bad-{i}-{uuid4().hex[:6]}", "size_bytes": 1024}
        job = _create(client, admin, "checksum", payload)["job_id"]
        jobs.append(job)
        _dispatch(client, admin, job, n)
        assert _accept(n, job).status_code == 200
        forged = dict(cpu_data.run_checksum(payload), output_sha256="ab" * 32)
        s = _submit(n, job, forged)
        assert s.status_code == 200, s.text
        assert s.json()["verification"]["verification_status"] == "FAILED"
        assert s.json()["status"] == "QUEUED"  # requeued for another attempt
        assert s.json()["verification"]["test_earnings_credited"] is False

    info = _job(client, admin, jobs[0])
    assert info["retry_count"] == 1 and info["last_error"].startswith("recompute mismatch")
    attempts = client.get(f"{BASE}/admin/jobs/{jobs[0]}/attempts", headers=admin).json()
    assert attempts["attempts"][0]["outcome"] == "INVALID_RESULT"
    trail = [(e["from"], e["to"]) for e in attempts["events"]]
    assert ("VERIFYING", "INVALID_RESULT") in trail and ("INVALID_RESULT", "QUEUED") in trail

    dev = n.get(admin)
    assert dev["status"] == "QUARANTINED"
    rep1 = float(_sql("SELECT reputation_score FROM devices WHERE id = :i", i=n.id)[0][0])
    assert rep1 < rep0
    assert _earnings(client, admin, n) == 0.0
    # A quarantined device can't get new work.
    j2 = _create(client, admin, "checksum", CHK)["job_id"]
    assert client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": j2, "device_id": n.id}).status_code == 409


def test_accept_timeout_requeues_then_dead_letters(client, admin, monkeypatch):
    n = _ready_node(client, admin)
    monkeypatch.setattr(settings, "LEASE_ACCEPT_TIMEOUT_SECONDS", -120)
    monkeypatch.setattr(settings, "LEASE_GRACE_SECONDS", 0)
    job = _create(client, admin, "checksum", CHK, max_retries=1)["job_id"]

    _dispatch(client, admin, job, n)
    sw = client.post(f"{BASE}/admin/jobs/sweep-leases", headers=admin).json()
    assert any(s["job_id"] == job and s["job_status"] == "QUEUED" for s in sw["swept"])
    info = _job(client, admin, job)
    assert info["status"] == "QUEUED" and info["retry_count"] == 1 and info["assignment"] is None
    r = _accept(n, job)
    assert r.status_code == 409 and "expired" in r.text

    _dispatch(client, admin, job, n)  # attempt 2 (retry budget = 1)
    _poll(n)  # opportunistic sweep on poll
    info = _job(client, admin, job)
    assert info["status"] == "FAILED" and info["dead_lettered"] is True
    dl = client.get(f"{BASE}/admin/jobs/dead-letter", headers=admin).json()
    assert any(j["job_id"] == job for j in dl["jobs"])
    attempts = client.get(f"{BASE}/admin/jobs/{job}/attempts", headers=admin).json()
    assert [a["status"] for a in attempts["attempts"]] == ["EXPIRED", "EXPIRED"]
    assert any(e["reason"] and e["reason"].startswith("dead-letter") for e in attempts["events"])

    rq = client.post(f"{BASE}/admin/jobs/{job}/requeue", headers=admin)
    assert rq.status_code == 200 and rq.json()["status"] == "QUEUED"
    assert _job(client, admin, job)["dead_lettered"] is False


def test_late_result_after_lease_expiry_rejected_without_credit(client, admin, monkeypatch):
    n = _ready_node(client, admin)
    job = _create(client, admin, "checksum", CHK, max_runtime_seconds=1)["job_id"]
    _dispatch(client, admin, job, n)
    monkeypatch.setattr(settings, "LEASE_RUNTIME_BUFFER_SECONDS", -100)
    monkeypatch.setattr(settings, "LEASE_GRACE_SECONDS", 0)
    assert _accept(n, job).status_code == 200
    _sql("UPDATE job_leases SET grace_seconds = 0 WHERE job_id = :j", j=job)
    before = _earnings(client, admin, n)
    s = _submit(n, job, cpu_data.run_checksum(CHK))
    assert s.status_code == 409 and "late result" in s.text
    info = _job(client, admin, job)
    assert info["status"] == "QUEUED" and info["retry_count"] == 1
    assert _earnings(client, admin, n) == before
    assert n.get(admin)["status"] in ("IDLE", "ACTIVE")
    # Retrying the same late submission is still rejected (no credit path).
    assert _submit(n, job, cpu_data.run_checksum(CHK)).status_code == 409


def test_device_reported_failure_soft_retry(client, admin):
    n = _ready_node(client, admin)
    job = _create(client, admin, "http_check", {"url": "https://example.com"})["job_id"]
    _dispatch(client, admin, job, n)
    assert _accept(n, job).status_code == 200
    s = _submit(n, job, {"status": "FAILED", "error": "dns failure"})
    assert s.status_code == 200
    assert s.json()["status"] == "QUEUED"
    trail = [(e["from"], e["to"]) for e in client.get(f"{BASE}/admin/jobs/{job}/attempts", headers=admin).json()["events"]]
    assert ("VERIFYING", "FAILED") in trail and ("FAILED", "QUEUED") in trail
    assert n.get(admin)["status"] == "ACTIVE"


def test_idempotency_key_on_create(client, admin):
    key = uuid4().hex
    a = _create(client, admin, "checksum", CHK, headers={"Idempotency-Key": key})
    b = _create(client, admin, "checksum", CHK, headers={"Idempotency-Key": key})
    assert a["job_id"] == b["job_id"] and b["idempotent_replay"] is True
    bad = client.post(f"{BASE}/jobs/", headers=admin, json={"workload_type": "checksum", "payload": {"seed": "x"}})
    assert bad.status_code == 400


def _http_result(code):
    return {"status": "COMPLETED", "http_status": code, "duration_ms": 40, "bytes_transferred": 10,
            "url": "https://example.com", "method": "GET"}


def _run_redundant(client, admin, monkeypatch, shadow_code):
    monkeypatch.setattr(settings, "REDUNDANT_SAMPLE_RATE", 1.0)
    a, b = _ready_node(client, admin), _ready_node(client, admin)
    _isolate(a, b)
    job = _create(client, admin, "http_check", {"url": "https://example.com"})["job_id"]
    _dispatch(client, admin, job, a)
    assert _accept(a, job).status_code == 200
    s = _submit(a, job, _http_result(200))
    assert s.status_code == 200, s.text
    v = s.json()["verification"]
    assert v["verification_status"] == "PENDING" and s.json()["status"] == "VERIFYING"
    assert v["test_earnings_credited"] is False

    polled = _poll(b)
    shadow = polled["job"]["job_id"]
    assert shadow != job and polled["job"]["payload"] == {"url": "https://example.com"}
    assert _accept(b, shadow).status_code == 200
    s2 = _submit(b, shadow, _http_result(shadow_code))
    assert s2.status_code == 200, s2.text
    return a, b, job, shadow, s2.json()


def test_redundant_execution_agreement_credits_primary_only(client, admin, monkeypatch):
    a, b, job, shadow, _ = _run_redundant(client, admin, monkeypatch, 204)
    info = _job(client, admin, job)
    assert info["status"] == "VERIFIED"
    assert info["verification"]["method"] == "SCHEMA+REDUNDANT_SAMPLE"
    assert info["verification"]["confidence"] == "HIGH"
    assert _earnings(client, admin, a) == pytest.approx(0.0001)
    assert _earnings(client, admin, b) == 0.0  # internal shadow work is never credited
    assert _job(client, admin, shadow)["role"] == "REDUNDANT"


def test_redundant_execution_disagreement_requeues(client, admin, monkeypatch):
    a, b, job, shadow, _ = _run_redundant(client, admin, monkeypatch, 503)
    info = _job(client, admin, job)
    assert info["status"] == "QUEUED" and info["retry_count"] == 1
    assert "redundant mismatch" in info["last_error"]
    assert _earnings(client, admin, a) == 0.0


def test_redundant_fallback_when_no_second_device(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "REDUNDANT_SAMPLE_RATE", 1.0)
    a = _ready_node(client, admin)
    _isolate(a)
    job = _create(client, admin, "http_check", {"url": "https://example.com"})["job_id"]
    _dispatch(client, admin, job, a)
    assert _accept(a, job).status_code == 200
    s = _submit(a, job, _http_result(200)).json()
    assert s["status"] == "VERIFIED" and s["verification"]["confidence"] == "LOW"


def test_canary_spot_check(client, admin):
    good, bad = _ready_node(client, admin), _ready_node(client, admin)
    c1 = client.post(f"{BASE}/admin/jobs/canary", headers=admin, json={"device_id": good.id, "workload_type": "checksum"})
    assert c1.status_code == 200, c1.text
    job = c1.json()["job_id"]
    polled = _poll(good)
    assert polled["job"]["job_id"] == job and "expected" not in json.dumps(polled)
    assert _accept(good, job).status_code == 200
    s = _submit(good, job, cpu_data.run_checksum(polled["job"]["payload"])).json()
    assert s["status"] == "VERIFIED" and s["verification"]["method"].startswith("HASH")
    assert _earnings(client, admin, good) == 0.0  # canaries never pay

    c2 = client.post(f"{BASE}/admin/jobs/canary", headers=admin, json={"device_id": bad.id, "workload_type": "checksum"})
    job2 = c2.json()["job_id"]
    p2 = _poll(bad)["job"]["payload"]
    assert _accept(bad, job2).status_code == 200
    forged = dict(cpu_data.run_checksum(p2), output_sha256="cd" * 32)
    s2 = _submit(bad, job2, forged).json()
    assert s2["verification"]["verification_status"] == "FAILED"
    assert s2["verification"]["reputation"]["quarantined"] is True
    assert bad.get(admin)["status"] == "QUARANTINED"
    # Explicit admin release restores probation reputation.
    assert client.post(f"{BASE}/admin/devices/{bad.id}/release", headers=admin).status_code == 200
    rep_after = float(_sql("SELECT reputation_score FROM devices WHERE id = :i", i=bad.id)[0][0])
    assert rep_after >= settings.REPUTATION_QUARANTINE_BELOW


def test_compression_and_json_transform_end_to_end(client, admin):
    n = _ready_node(client, admin)
    for wt, payload, runner in [
        ("compression", {"seed": "cz", "size_bytes": 32768}, cpu_data.run_compression),
        ("json_transform", {"document": [{"g": "a", "v": 3}, {"g": "b", "v": 4}, {"g": "a", "v": 5}],
                            "ops": [{"op": "filter_eq", "field": "g", "value": "a"}, {"op": "sum", "field": "v"}]},
         cpu_data.run_json_transform),
    ]:
        job = _create(client, admin, wt, payload)["job_id"]
        _dispatch(client, admin, job, n)
        assert _accept(n, job).status_code == 200
        s = _submit(n, job, runner(payload))
        assert s.status_code == 200 and s.json()["status"] == "VERIFIED", s.text
