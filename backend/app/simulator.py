"""FARMOS Node Simulator — signs enrollment and heartbeat like a real node."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)

DEVICE_TEMPLATES = [
    {"manufacturer": "Samsung", "model": "SM-G991U", "cpu_cores": 8, "ram_mb": 8192, "storage_total_mb": 128000},
    {"manufacturer": "Google", "model": "Pixel 7", "cpu_cores": 8, "ram_mb": 8192, "storage_total_mb": 128000},
    {"manufacturer": "Xiaomi", "model": "Redmi Note 11", "cpu_cores": 6, "ram_mb": 4096, "storage_total_mb": 64000},
]


def sign_payload(private_key: ec.EllipticCurvePrivateKey, payload: dict) -> str:
    import json

    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


class SimNode:
    def __init__(self, api_base: str, admin_token: str, user_id: str):
        self.api_base = api_base
        self.admin_token = admin_token
        self.user_id = user_id
        self.device_id: str | None = None
        self.node_token: str | None = None
        self.profile = random.choice(DEVICE_TEMPLATES)
        self.temperature = 30.0
        self.running = False
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key_pem = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    async def run(self, *, once: bool = False):
        async with httpx.AsyncClient(timeout=10) as client:
            await self._enroll(client)
            if not self.device_id:
                return
            if once:
                await self._heartbeat(client)
                return
            while self.running:
                await self._heartbeat(client)
                await asyncio.sleep(random.uniform(5, 15))

    async def _enroll(self, client: httpx.AsyncClient):
        token_resp = await client.post(
            f"{self.api_base}/api/v1/devices/enrollment-token",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={},
        )
        if token_resp.status_code != 200:
            logger.warning("sim_enroll_token_failed status=%s body=%s", token_resp.status_code, token_resp.text[:200])
            return
        enroll_token = token_resp.json()["enrollment_token"]

        profile = dict(self.profile)
        profile["android_version"] = "14"
        profile["architecture"] = "arm64-v8a"
        profile["capabilities"] = ["cpu_compute", "ai_inference", "network", "storage"]
        payload = {
            "enrollment_token": enroll_token,
            "device_public_key": self.public_key_pem,
            "hardware_fingerprint": f"sha256:{uuid4().hex}",
            "profile": profile,
            "request_nonce": uuid4().hex,
            "request_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload["signature"] = sign_payload(self.private_key, payload)
        enroll_resp = await client.post(
            f"{self.api_base}/api/v1/devices/enroll",
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        if enroll_resp.status_code == 200:
            data = enroll_resp.json()
            self.device_id = data.get("device_id")
            self.node_token = data.get("node_token")
            logger.info("sim_device_enrolled device_id=%s human_id=%s", self.device_id, data.get("human_id"))
        else:
            logger.warning("sim_enroll_failed status=%s body=%s", enroll_resp.status_code, enroll_resp.text[:200])

    async def _heartbeat(self, client: httpx.AsyncClient):
        if not self.device_id or not self.node_token:
            return
        self.temperature = max(28.0, min(45.0, self.temperature + random.uniform(-0.5, 0.8)))
        payload = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "battery_pct": random.uniform(40, 95),
            "charging": random.random() > 0.3,
            "temperature_c": self.temperature,
            "cpu_util_pct": random.uniform(5, 60),
            "ram_used_mb": random.randint(1024, 6144),
            "storage_free_mb": random.randint(10000, 42000),
            "network": {"type": "wifi", "down_mbps": random.uniform(50, 400), "up_mbps": random.uniform(10, 60)},
            "app_version": "1.2.0",
            "request_nonce": uuid4().hex,
            "request_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload["signature"] = sign_payload(self.private_key, payload)
        resp = await client.post(
            f"{self.api_base}/api/v1/devices/{self.device_id}/heartbeat",
            headers={"Authorization": f"Bearer {self.node_token}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            logger.warning("sim_heartbeat_failed status=%s body=%s", resp.status_code, resp.text[:200])
        else:
            logger.info("sim_heartbeat_ok device_id=%s", self.device_id)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_base = os.environ.get("FARMOS_API_URL", "http://localhost:8000")
    admin_email = os.environ.get("FARMOS_SIM_ADMIN_EMAIL", "admin@example.com")
    admin_password = os.environ.get("FARMOS_SIM_ADMIN_PASSWORD", "ChangeMe123!")

    async with httpx.AsyncClient(timeout=15) as client:
        login = await client.post(
            f"{api_base}/api/v1/auth/login",
            headers={"Content-Type": "application/json"},
            json={"email": admin_email, "password": admin_password},
        )
        if login.status_code != 200:
            logger.error("sim_owner_login_failed status=%s body=%s", login.status_code, login.text[:200])
            return
        data = login.json()
        admin_token = data["access_token"]
        user_id = data["user_id"]

    count = int(os.environ.get("FARMOS_SIM_COUNT", "5"))
    once = os.environ.get("FARMOS_SIM_ONESHOT", "").lower() in {"1", "true", "yes"}
    logger.info("sim_starting nodes=%s once=%s", count, once)
    nodes = [SimNode(api_base, admin_token, user_id) for _ in range(count)]
    for n in nodes:
        n.running = True
    tasks = [asyncio.create_task(n.run(once=once)) for n in nodes]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
