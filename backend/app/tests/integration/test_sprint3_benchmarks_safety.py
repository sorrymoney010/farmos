"""Sprint 3 integration: benchmarks, FarmScore, thermal/battery safety governor.

Runs against the live Postgres + Redis stack like the other integration tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.config import settings

BASE = "/api/v1"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "ChangeMe123!"

GOOD_RAW = {
    "cpu": {"single_thread_hashes_per_sec": 120000, "multi_thread_hashes_per_sec": 700000, "threads": 8},
    "ai": {"matmul_gflops": 1.8},
    "memory": {"copy_mb_per_sec": 3500.5, "total_mb": 4096, "available_mb": 1500},
    "storage": {"write_mb_per_sec": 80.25, "read_mb_per_sec": 210.0, "free_mb": 20000},
    "network": {"latency_ms": 45.3, "down_mbps": None, "up_mbps": None},
    "thermal": {"start_c": 31.2, "end_c": 33.0, "peak_c": 33.4},
    "battery": {"pct": 88, "charging": True, "health": "GOOD", "temperature_c": 31.2},
    "duration_ms": 9000,
}


def _sign(key, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(key.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    r = client.post(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class Node:
    def __init__(self, client, admin, model="Pixel 8"):
        self.c = client
        self.key = ec.generate_private_key(ec.SECP256R1())
        tok = client.post(f"{BASE}/devices/enrollment-token", headers=admin, json={}).json()["enrollment_token"]
        pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        payload = {
            "enrollment_token": tok,
            "device_public_key": pem,
            "hardware_fingerprint": f"sha256:{uuid4().hex}",
            "profile": {"manufacturer": "Google", "model": model, "cpu_cores": 8, "ram_mb": 4096},
            "request_nonce": uuid4().hex,
            "request_timestamp": _now(),
        }
        payload["signature"] = _sign(self.key, payload)
        r = client.post(f"{BASE}/devices/enroll", json=payload)
        assert r.status_code == 200, r.text
        self.id = r.json()["device_id"]
        self.token = r.json()["node_token"]
        self.h = {"Authorization": f"Bearer {self.token}"}

    def heartbeat(self, **telemetry):
        hb = {"observed_at": _now(), "request_nonce": uuid4().hex, "request_timestamp": _now(), **telemetry}
        hb["signature"] = _sign(self.key, hb)
        return self.c.post(f"{BASE}/devices/{self.id}/heartbeat", headers=self.h, json=hb)

    def benchmark(self, raw=None, job_id=None, tamper=False):
        raw_json = json.dumps(raw or GOOD_RAW, separators=(",", ":"))
        sha = hashlib.sha256(raw_json.encode()).hexdigest()
        body = {
            "suite_version": "farmos-bench-1",
            "job_id": job_id,
            "raw_results_json": raw_json,
            "results_sha256": sha,
            "request_nonce": uuid4().hex,
            "request_timestamp": _now(),
        }
        signed = {k: body[k] for k in ("request_nonce", "request_timestamp", "suite_version", "results_sha256")}
        signed["device_id"] = self.id
        signed["job_id"] = job_id or ""
        body["signature"] = _sign(self.key, signed)
        if tamper:
            body["raw_results_json"] = raw_json.replace("700000", "9700000")
        return self.c.post(f"{BASE}/devices/{self.id}/benchmark/result", headers=self.h, json=body)

    def get(self, admin):
        return self.c.get(f"{BASE}/devices/{self.id}", headers=admin).json()


GREEN = {"temperature_c": 31.0, "battery_pct": 90.0, "charging": True, "battery_health": "GOOD"}


def _activate(node):
    assert node.heartbeat(**GREEN).status_code == 200
    r = node.benchmark()
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACTIVE"


def _http_job(client, admin):
    r = client.post(f"{BASE}/jobs/", headers=admin, json={"workload_type": "http_check", "payload": {"url": "https://example.com"}})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def test_benchmark_gate_then_active_with_farmscore(client, admin):
    n = Node(client, admin)
    r = n.heartbeat(**GREEN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "BENCHMARKING"
    assert body["benchmark_required"] is True
    assert body["safety"]["temperature_state"] == "GREEN"

    # Not dispatchable for real work until the baseline passes.
    job = _http_job(client, admin)
    d = client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job, "device_id": n.id})
    assert d.status_code == 409 and "benchmark must pass" in d.text

    b = n.benchmark()
    assert b.status_code == 200, b.text
    res = b.json()
    assert res["passed_baseline"] is True and res["status"] == "ACTIVE"
    assert 0 < res["farm_score"] <= 100
    assert set(res["scores"]) == {"cpu", "ai", "memory", "storage", "network", "thermal",
                                  "power_efficiency", "reliability"}
    assert abs(sum(res["weights"].values()) - 1) < 1e-6

    dev = n.get(admin)
    assert dev["farm_score"] == pytest.approx(res["farm_score"])
    assert dev["latest_benchmark"]["benchmark_id"] == res["benchmark_id"]
    hist = client.get(f"{BASE}/devices/{n.id}/benchmarks", headers=admin).json()
    assert len(hist["benchmarks"]) == 1

    d = client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job, "device_id": n.id})
    assert d.status_code == 200, d.text


def test_benchmark_rejects_bad_signature_tamper_and_replay(client, admin):
    n = Node(client, admin)
    n.heartbeat(**GREEN)
    assert n.benchmark(tamper=True).status_code == 400  # sha mismatch
    other = Node(client, admin)
    raw_json = json.dumps(GOOD_RAW)
    body = {"suite_version": "farmos-bench-1", "job_id": None, "raw_results_json": raw_json,
            "results_sha256": hashlib.sha256(raw_json.encode()).hexdigest(),
            "request_nonce": uuid4().hex, "request_timestamp": _now()}
    signed = {"request_nonce": body["request_nonce"], "request_timestamp": body["request_timestamp"],
              "device_id": n.id, "suite_version": body["suite_version"], "job_id": "",
              "results_sha256": body["results_sha256"]}
    body["signature"] = _sign(other.key, signed)  # wrong key
    r = client.post(f"{BASE}/devices/{n.id}/benchmark/result", headers=n.h, json=body)
    assert r.status_code == 401
    body["signature"] = _sign(n.key, signed)
    assert client.post(f"{BASE}/devices/{n.id}/benchmark/result", headers=n.h, json=body).status_code == 200
    assert client.post(f"{BASE}/devices/{n.id}/benchmark/result", headers=n.h, json=body).status_code == 409


def test_spoofed_benchmark_does_not_pass_baseline(client, admin):
    n = Node(client, admin)
    n.heartbeat(**GREEN)
    raw = json.loads(json.dumps(GOOD_RAW))
    raw["cpu"]["multi_thread_hashes_per_sec"] = 9e12
    r = n.benchmark(raw)
    assert r.status_code == 200
    assert r.json()["passed_baseline"] is False and r.json()["anomalies"]
    assert r.json()["status"] == "BENCHMARKING"


def test_benchmark_job_flow(client, admin):
    n = Node(client, admin)
    _activate(n)
    r = client.post(f"{BASE}/devices/{n.id}/benchmark", headers=admin, json={})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    polled = client.post(f"{BASE}/jobs/device/jobs/next", headers=n.h).json()
    assert polled["job"]["workload_type"] == "benchmark"
    b = n.benchmark(job_id=job_id)
    assert b.status_code == 200, b.text
    job = client.get(f"{BASE}/jobs/{job_id}", headers=admin).json()
    assert job["status"] == "VERIFIED"


def test_thermal_red_pauses_dispatch_then_resumes_after_cooldown(client, admin, monkeypatch):
    n = Node(client, admin)
    _activate(n)
    job = _http_job(client, admin)
    assert client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job, "device_id": n.id}).status_code == 200

    hot = n.heartbeat(temperature_c=46.5, battery_pct=90.0, charging=True, battery_health="GOOD")
    assert hot.status_code == 200
    s = hot.json()
    assert s["status"] == "COOLING"
    assert s["safety"]["paused"] is True and s["safety"]["temperature_state"] == "COOLING"

    # Already-dispatched job is withheld at poll time (defense in depth).
    polled = client.post(f"{BASE}/jobs/device/jobs/next", headers=n.h).json()
    assert polled["job"] is None and polled["paused"] is True

    job2 = _http_job(client, admin)
    d = client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job2, "device_id": n.id})
    assert d.status_code == 409 and "cooling" in d.text.lower()

    # Cooled but cooldown window not passed -> stays COOLING.
    assert n.heartbeat(**GREEN).json()["status"] == "COOLING"

    # Expire the window (simulate time passing) -> GREEN resumes to IDLE.
    import asyncio

    from sqlalchemy import update

    from app.db.models.devices import Device
    from app.db.session import AsyncSessionLocal

    async def expire():
        async with AsyncSessionLocal() as s:
            await s.execute(update(Device).where(Device.id == n.id).values(
                cooldown_until=datetime(2000, 1, 1, tzinfo=timezone.utc)))
            await s.commit()

    asyncio.run(expire())
    resumed = n.heartbeat(**GREEN).json()
    assert resumed["status"] in {"IDLE", "ACTIVE"} and resumed["safety"]["paused"] is False
    polled = client.post(f"{BASE}/jobs/device/jobs/next", headers=n.h).json()
    assert polled["job"]["job_id"] == job


def test_orange_allows_light_blocks_benchmark_and_low_battery_pauses(client, admin):
    n = Node(client, admin)
    _activate(n)
    r = n.heartbeat(temperature_c=43.0, battery_pct=90.0, charging=True, battery_health="GOOD").json()
    assert r["status"] == "THROTTLED" and r["safety"]["workload_class"] == "LIGHT"
    b = client.post(f"{BASE}/devices/{n.id}/benchmark", headers=admin, json={})
    assert b.status_code == 409
    job = _http_job(client, admin)
    assert client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job, "device_id": n.id}).status_code == 200

    low = n.heartbeat(temperature_c=30.0, battery_pct=25.0, charging=False, battery_health="GOOD").json()
    assert low["safety"]["paused"] is True and "battery" in low["safety"]["reason"]
    job2 = _http_job(client, admin)
    assert client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job2, "device_id": n.id}).status_code == 409


def test_android_thermal_status_and_battery_temp_count(client, admin):
    n = Node(client, admin)
    _activate(n)
    r = n.heartbeat(temperature_c=30.0, battery_temperature_c=30.0, thermal_status=4,
                    battery_pct=90.0, charging=True, battery_health="GOOD").json()
    assert r["status"] == "COOLING"


def test_battery_fault_quarantines_and_admin_release(client, admin):
    n = Node(client, admin)
    _activate(n)
    r = n.heartbeat(temperature_c=30.0, battery_pct=90.0, charging=True, battery_health="OVER_VOLTAGE").json()
    assert r["status"] == "QUARANTINED" and r["safety"]["emergency_stop"] is True
    # Healthy heartbeat never auto-releases.
    assert n.heartbeat(**GREEN).json()["status"] == "QUARANTINED"
    rel = client.post(f"{BASE}/admin/devices/{n.id}/release", headers=admin)
    assert rel.status_code == 200, rel.text
    assert n.heartbeat(**GREEN).json()["status"] in {"IDLE", "ACTIVE"}


def test_legacy_heartbeat_without_new_fields_still_verifies(client, admin):
    n = Node(client, admin)
    r = n.heartbeat(battery_pct=80.0, charging=True, temperature_c=32.0, cpu_util_pct=10.0,
                    ram_used_mb=1024, storage_free_mb=32000,
                    network={"type": "wifi", "down_mbps": 100.0, "up_mbps": 20.0}, app_version="1.0")
    assert r.status_code == 200, r.text


def test_admin_weights_and_policy(client, admin):
    g = client.get(f"{BASE}/admin/farmscore/weights", headers=admin)
    assert g.status_code == 200 and abs(sum(g.json()["weights"].values()) - 1) < 1e-6
    bad = client.put(f"{BASE}/admin/farmscore/weights", headers=admin, json={"weights": {"cpu": -1}})
    assert bad.status_code == 400
    try:
        ok = client.put(f"{BASE}/admin/farmscore/weights", headers=admin,
                        json={"weights": {"cpu": 1, "network": 1}})
        assert ok.status_code == 200, ok.text
        assert ok.json()["weights"]["cpu"] == pytest.approx(0.5)
    finally:
        client.put(f"{BASE}/admin/farmscore/weights", headers=admin,
                   json={"weights": settings.FARMSCORE_WEIGHTS})

    bad = client.put(f"{BASE}/admin/safety/policy", headers=admin,
                     json={"default": {"green_max": 50, "yellow_max": 40}})
    assert bad.status_code == 400
    try:
        ok = client.put(f"{BASE}/admin/safety/policy", headers=admin,
                        json={"models": {"Stratus C7": {"green_max": 36, "yellow_max": 40, "orange_max": 43}}})
        assert ok.status_code == 200, ok.text
        n = Node(client, admin, model="Stratus C7")
        r = n.heartbeat(temperature_c=37.0, battery_pct=90.0, charging=True, battery_health="GOOD").json()
        assert r["safety"]["temperature_state"] == "YELLOW"  # per-model override applied
    finally:
        client.put(f"{BASE}/admin/safety/policy", headers=admin, json={})


def test_verify_after_overheat_does_not_override_cooling(client, admin):
    n = Node(client, admin)
    _activate(n)
    job = _http_job(client, admin)
    assert client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": job, "device_id": n.id}).status_code == 200
    acc = {"request_nonce": uuid4().hex, "request_timestamp": _now(), "job_id": job, "device_id": n.id}
    sig = _sign(n.key, acc)
    r = client.post(f"{BASE}/jobs/device/jobs/{job}/accept", headers=n.h,
                    json={"request_nonce": acc["request_nonce"], "request_timestamp": acc["request_timestamp"],
                          "signature": sig})
    assert r.status_code == 200, r.text
    assert n.get(admin)["status"] == "RUNNING"

    result_uri = json.dumps({"status": "COMPLETED", "http_status": 200, "duration_ms": 12.0})
    res = {"request_nonce": uuid4().hex, "request_timestamp": _now(), "job_id": job, "device_id": n.id,
           "result_hash": hashlib.sha256(result_uri.encode()).hexdigest(), "result_uri": result_uri}
    body = {k: res[k] for k in ("request_nonce", "request_timestamp", "result_hash", "result_uri")}
    body["signature"] = _sign(n.key, res)
    body["execution_metrics"] = {"peak_temperature_c": 46.0}
    assert client.post(f"{BASE}/jobs/device/jobs/{job}/result", headers=n.h, json=body).status_code == 200

    assert n.heartbeat(temperature_c=46.0, battery_pct=90.0, charging=True,
                       battery_health="GOOD").json()["status"] == "COOLING"
    v = client.post(f"{BASE}/jobs/verify/result", headers=admin, json={"job_id": job})
    assert v.status_code == 200, v.text
    assert n.get(admin)["status"] == "COOLING"
