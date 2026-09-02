"""Sprint 11 integration tests: Android secure node enrollment + signed heartbeat.

These run against the live Postgres + Redis stack (same as the rest of the
integration suite). They rely on the seeded Phase 1 admin (admin@example.com)
to issue one-time enrollment tokens. No hardware/Android device is required.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.security.node_auth import verify_request_signature

pytest_plugins = ["pytest_asyncio"]

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "ChangeMe123!"

BASE = "/api/v1"


def _sign(private_key, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(private_key.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


def _new_key():
    return ec.generate_private_key(ec.SECP256R1())


def _public_pem(private_key) -> str:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@pytest.fixture
def client():
    from app.main import app

    # Use the real DB/Redis stack (same as other integration tests).
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_token(client):
    resp = client.post(
        f"{BASE}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _issue_enrollment_token(client, admin_token, expires_in_minutes=15):
    resp = client.post(
        f"{BASE}/devices/enrollment-token",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"expires_in_minutes": expires_in_minutes},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["enrollment_token"]


def _build_enroll_payload(private_key, enrollment_token, timestamp=None, public_key_pem=None):
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    pem = public_key_pem or _public_pem(private_key)
    payload = {
        "enrollment_token": enrollment_token,
        "device_public_key": pem,
        "hardware_fingerprint": f"sha256:{uuid4().hex}",
        "profile": {
            "manufacturer": "Google",
            "model": "Pixel 8",
            "android_version": "14",
            "architecture": "arm64-v8a",
            "cpu_cores": 8,
            "ram_mb": 8192,
            "storage_total_mb": 128000,
            "capabilities": ["cpu_compute", "network", "storage"],
        },
        "request_nonce": uuid4().hex,
        "request_timestamp": ts,
    }
    payload["signature"] = _sign(private_key, payload)
    return payload


def _enroll(client, admin_token, enroll_token, private_key=None, **kwargs):
    key = private_key or _new_key()
    payload = _build_enroll_payload(key, enroll_token, **kwargs)
    resp = client.post(f"{BASE}/devices/enroll", json=payload)
    return resp, key, payload


def _build_heartbeat(device_id, node_token, timestamp=None, nonce=None):
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    payload = {
        "observed_at": ts,
        "battery_pct": 82.0,
        "charging": True,
        "temperature_c": 31.5,
        "cpu_util_pct": 12.5,
        "ram_used_mb": 1500,
        "storage_free_mb": 42000,
        "network": {"type": "wifi", "down_mbps": 120.0, "up_mbps": 24.0},
        "app_version": "1.0.0",
        "request_nonce": nonce or uuid4().hex,
        "request_timestamp": ts,
    }
    return payload


def test_valid_signed_enrollment(client, admin_token):
    token = _issue_enrollment_token(client, admin_token)
    resp, _key, _payload = _enroll(client, admin_token, token)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "device_id" in body
    assert "node_token" in body
    assert body["status"] == "BENCHMARKING"


def test_reused_enrollment_token_rejected(client, admin_token):
    token = _issue_enrollment_token(client, admin_token)
    first, _key, _payload = _enroll(client, admin_token, token)
    assert first.status_code == 200
    second, _k2, _p2 = _enroll(client, admin_token, token)
    assert second.status_code == 400, second.text
    assert "Invalid or expired enrollment token" in second.text


def test_golden_signature_fixture():
    # Shared cross-language fixture: Python emits the canonical form that the
    # Android app must reproduce (sorted keys, no whitespace). This test proves
    # the fixture's signature verifies under Python's canonicalization, which is
    # the contract both sides must honor.
    import os

    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "signature_fixture.json")
    with open(fixture_path) as f:
        fixture = json.load(f)
    verify_request_signature(
        fixture["device_public_key_pem"],
        fixture["payload"],
        fixture["signature_base64"],
    )



def test_invalid_signature_does_not_consume_token(client, admin_token):
    # An invalid signed request must NOT burn the token: a subsequent valid
    # signed request with the same token must still succeed.
    token = _issue_enrollment_token(client, admin_token)
    real_key = _new_key()
    wrong_key = _new_key()
    bad_payload = _build_enroll_payload(real_key, token)
    bad_payload["signature"] = _sign(wrong_key, bad_payload)
    bad = client.post(f"{BASE}/devices/enroll", json=bad_payload)
    assert bad.status_code == 401, bad.text
    assert "Invalid device signature" in bad.text

    # Same token, now correctly signed, should enroll.
    good, _key, _payload = _enroll(client, admin_token, token)
    assert good.status_code == 200, good.text
    assert "device_id" in good.json()


def test_concurrent_enrollment_token_single_use(client, admin_token):
    # Prove atomic GETDEL: exactly one of two near-simultaneous enrollments with the
    # same token succeeds; the other gets "Invalid or expired enrollment token".
    import asyncio

    token = _issue_enrollment_token(client, admin_token)

    async def _attempt(key, loop):
        payload = _build_enroll_payload(key, token)
        # Run inside the TestClient (sync) from the event loop thread.
        return await loop.run_in_executor(None, lambda: client.post(f"{BASE}/devices/enroll", json=payload))

    async def _race():
        loop = asyncio.get_event_loop()
        k1, k2 = _new_key(), _new_key()
        r1, r2 = await asyncio.gather(_attempt(k1, loop), _attempt(k2, loop))
        return r1, r2

    r1, r2 = asyncio.run(_race())
    statuses = {r1.status_code, r2.status_code}
    # Exactly one 200, the other 400 invalid/expired token.
    assert statuses == {200, 400}, f"unexpected: {r1.status_code} {r2.status_code}"
    assert (r1.status_code == 200) ^ (r2.status_code == 200)
    texts = (r1.text + r2.text)
    assert "Invalid or expired enrollment token" in texts



def test_expired_timestamp_rejected(client, admin_token):
    token = _issue_enrollment_token(client, admin_token)
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    resp, _key, _payload = _enroll(client, admin_token, token, timestamp=old_ts)
    assert resp.status_code == 400, resp.text
    assert "outside allowed skew" in resp.text


def test_valid_node_token_heartbeat_flips_active(client, admin_token):
    token = _issue_enrollment_token(client, admin_token)
    enroll_resp, key, _payload = _enroll(client, admin_token, token)
    assert enroll_resp.status_code == 200
    data = enroll_resp.json()
    device_id = data["device_id"]
    node_token = data["node_token"]

    hb = _build_heartbeat(device_id, node_token)
    hb["signature"] = _sign(key, hb)
    resp = client.post(
        f"{BASE}/devices/{device_id}/heartbeat",
        headers={"Authorization": f"Bearer {node_token}"},
        json=hb,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ACTIVE"

    dev = client.get(
        f"{BASE}/devices/{device_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert dev.status_code == 200
    assert dev.json()["status"] == "ACTIVE"


def test_node_token_device_mismatch_rejected(client, admin_token):
    token_a = _issue_enrollment_token(client, admin_token)
    enroll_a, key_a, _ = _enroll(client, admin_token, token_a)
    device_a = enroll_a.json()["device_id"]
    node_a = enroll_a.json()["node_token"]

    token_b = _issue_enrollment_token(client, admin_token)
    enroll_b, key_b, _ = _enroll(client, admin_token, token_b)
    device_b = enroll_b.json()["device_id"]

    hb = _build_heartbeat(device_b, node_a)
    hb["signature"] = _sign(key_a, hb)
    resp = client.post(
        f"{BASE}/devices/{device_b}/heartbeat",
        headers={"Authorization": f"Bearer {node_a}"},
        json=hb,
    )
    assert resp.status_code == 403, resp.text
    assert "Node token/device mismatch" in resp.text


def test_heartbeat_replay_rejected(client, admin_token):
    token = _issue_enrollment_token(client, admin_token)
    enroll_resp, key, _ = _enroll(client, admin_token, token)
    device_id = enroll_resp.json()["device_id"]
    node_token = enroll_resp.json()["node_token"]

    nonce = uuid4().hex
    hb = _build_heartbeat(device_id, node_token, nonce=nonce)
    hb["signature"] = _sign(key, hb)
    first = client.post(
        f"{BASE}/devices/{device_id}/heartbeat",
        headers={"Authorization": f"Bearer {node_token}"},
        json=hb,
    )
    assert first.status_code == 200
    second = client.post(
        f"{BASE}/devices/{device_id}/heartbeat",
        headers={"Authorization": f"Bearer {node_token}"},
        json=hb,
    )
    assert second.status_code == 409, second.text
    assert "Replay detected" in second.text
