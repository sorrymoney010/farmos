"""Acurast Processor Management Backend compatibility tests.

The official Acurast Processor app posts a P-256 signed check-in to the manager's
custom endpoint. FARMOS accepts only pre-registered processor addresses, verifies
the signature against the processor's SS58 address, and exposes current status.
"""

import hashlib
import json
from uuid import uuid4

from ecdsa import NIST256p, SigningKey
from ecdsa.util import sigencode_string
from fastapi.testclient import TestClient

BASE = "/api/v1"


def _base58_encode(value: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    integer = int.from_bytes(value, "big")
    output = ""
    while integer:
        integer, remainder = divmod(integer, 58)
        output = alphabet[remainder] + output
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + (output or "1")


def _acurast_address(key: SigningKey) -> str:
    point = key.verifying_key.pubkey.point
    compressed_key = bytes([2 + (point.y() & 1)]) + point.x().to_bytes(32, "big")
    public_key_hash = hashlib.blake2b(compressed_key, digest_size=32).digest()
    payload = bytes([42]) + public_key_hash
    checksum = hashlib.blake2b(b"SS58PRE" + payload, digest_size=64).digest()[:2]
    return _base58_encode(payload + checksum)


def _signed_check_in(key: SigningKey, address: str) -> tuple[dict, str]:
    payload = {
        "deviceAddress": address,
        "platform": 0,
        "timestamp": 1_725_739_200_000,
        "batteryLevel": 82,
        "isCharging": True,
        "networkType": "wifi",
        "ssid": "FARMOS-LAB",
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw_body).digest()
    signature = key.sign_digest_deterministic(
        digest,
        hashfunc=hashlib.sha256,
        sigencode=sigencode_string,
    )
    # The official protocol includes a recovery byte. The server recovers all
    # possible P-256 keys and validates the recovered SS58 address.
    return payload, (signature + b"\x00").hex()


def _admin_token(client: TestClient) -> str:
    response = client.post(
        f"{BASE}/auth/login",
        json={"email": "admin@example.com", "password": "ChangeMe123!"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_registered_acurast_processor_checkin_is_verified_and_visible():
    from app.main import app

    signing_key = SigningKey.generate(curve=NIST256p)
    address = _acurast_address(signing_key)
    payload, signature = _signed_check_in(signing_key, address)

    with TestClient(app) as client:
        admin_token = _admin_token(client)
        registered = client.post(
            f"{BASE}/acurast/processors",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"address": address, "label": "Stratus C7"},
        )
        assert registered.status_code == 201, registered.text

        check_in = client.post(
            "/processor/check-in",
            headers={"X-Device-Signature": signature},
            json=payload,
        )
        assert check_in.status_code == 200, check_in.text
        assert check_in.json() == {"success": True, "refreshIntervalInSeconds": 1800}

        status = client.get(f"/processor/api/{address}/status")
        assert status.status_code == 200, status.text
        processor_status = status.json()["processorStatus"]
        assert processor_status["address"] == address
        assert processor_status["batteryLevel"] == 82
        assert processor_status["isCharging"] is True
        assert processor_status["networkType"] == "wifi"


def test_unknown_acurast_processor_is_rejected_even_with_a_valid_signature():
    from app.main import app

    signing_key = SigningKey.generate(curve=NIST256p)
    address = _acurast_address(signing_key)
    payload, signature = _signed_check_in(signing_key, address)

    with TestClient(app) as client:
        response = client.post(
            "/processor/check-in",
            headers={"X-Device-Signature": signature},
            json=payload,
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Processor is not registered"


def test_acurast_checkin_rejects_a_tampered_body():
    from app.main import app

    signing_key = SigningKey.generate(curve=NIST256p)
    address = _acurast_address(signing_key)
    payload, signature = _signed_check_in(signing_key, address)
    payload["batteryLevel"] = 4

    with TestClient(app) as client:
        admin_token = _admin_token(client)
        registered = client.post(
            f"{BASE}/acurast/processors",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"address": address, "label": f"tamper-{uuid4().hex[:8]}"},
        )
        assert registered.status_code == 201, registered.text

        response = client.post(
            "/processor/check-in",
            headers={"X-Device-Signature": signature},
            json=payload,
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Acurast processor signature"
