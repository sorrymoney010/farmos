"""Claim-code wireless onboarding tests.

Extends Sprint 11 node-auth coverage: short claim codes exchange for enrollment
tokens; staging fixed code "123" is gated by STAGING_CLAIM_CODES.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.config import settings

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

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_token(client):
    resp = client.post(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _build_enroll_payload(private_key, enrollment_token, timestamp=None):
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    payload = {
        "enrollment_token": enrollment_token,
        "device_public_key": _public_pem(private_key),
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


def test_claim_code_exchange_and_enroll(client, admin_token):
    mint = client.post(
        f"{BASE}/devices/claim-code",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"expires_in_minutes": 15},
    )
    assert mint.status_code == 200, mint.text
    claim_code = mint.json()["claim_code"]
    assert len(claim_code) >= 4
    assert claim_code != "123"

    exch = client.post(f"{BASE}/devices/claim-code/exchange", json={"claim_code": claim_code})
    assert exch.status_code == 200, exch.text
    enrollment_token = exch.json()["enrollment_token"]
    assert enrollment_token.startswith("enroll-")

    # Claim code is one-time.
    again = client.post(f"{BASE}/devices/claim-code/exchange", json={"claim_code": claim_code})
    assert again.status_code == 400, again.text

    key = _new_key()
    payload = _build_enroll_payload(key, enrollment_token)
    enroll = client.post(f"{BASE}/devices/enroll", json=payload)
    assert enroll.status_code == 200, enroll.text
    assert "device_id" in enroll.json()
    assert "node_token" in enroll.json()


def test_staging_fixed_claim_code_123_gated(client, monkeypatch):
    # Off by default — "123" must not work in production-like config.
    monkeypatch.setattr(settings, "STAGING_CLAIM_CODES", False)
    denied = client.post(f"{BASE}/devices/claim-code/exchange", json={"claim_code": "123"})
    assert denied.status_code == 400, denied.text

    # Enable staging gate — "123" issues a real enrollment token for staging owner.
    monkeypatch.setattr(settings, "STAGING_CLAIM_CODES", True)
    if not settings.STAGING_ADMIN_USER_ID:
        monkeypatch.setattr(settings, "STAGING_ADMIN_USER_ID", "29443001-3ae8-40e3-91f8-969d97eda184")

    ok = client.post(f"{BASE}/devices/claim-code/exchange", json={"claim_code": "123"})
    assert ok.status_code == 200, ok.text
    token = ok.json()["enrollment_token"]
    assert token.startswith("enroll-")

    # Reusable in staging (convenience) — another exchange still works.
    ok2 = client.post(f"{BASE}/devices/claim-code/exchange", json={"claim_code": "123"})
    assert ok2.status_code == 200, ok2.text
    assert ok2.json()["enrollment_token"] != token


def test_staging_claim_code_mint_header(client, monkeypatch):
    monkeypatch.setattr(settings, "STAGING_ENROLL_ADMIN_SECRET", "test-staging-secret-value")
    monkeypatch.setattr(settings, "STAGING_CLAIM_CODES", True)
    bad = client.post(f"{BASE}/devices/staging/claim-code", json={})
    assert bad.status_code == 403

    good = client.post(
        f"{BASE}/devices/staging/claim-code",
        headers={"X-Staging-Enroll-Admin": "test-staging-secret-value"},
        json={"expires_in_minutes": 10},
    )
    assert good.status_code == 200, good.text
    body = good.json()
    assert "claim_code" in body
    assert body.get("staging_fixed_code") == "123"