"""FARMOS Node Simulator — connects fake devices to the running API."""

import asyncio
import json
import logging
import random
import time
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

DEVICE_TEMPLATES = [
    {"manufacturer": "Samsung", "model": "SM-G991U", "cpu_cores": 8, "ram_mb": 8192, "storage_total_mb": 128000},
    {"manufacturer": "Google", "model": "Pixel 7", "cpu_cores": 8, "ram_mb": 8192, "storage_total_mb": 128000},
    {"manufacturer": "Xiaomi", "model": "Redmi Note 11", "cpu_cores": 6, "ram_mb": 4096, "storage_total_mb": 64000},
]

CAPABILITIES = ["cpu_compute", "ai_inference", "network", "storage"]


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
        # Create provider for simulated user if needed (skipped for simplicity — reuse admin)
        token_resp = await client.post(
            f"{self.api_base}/api/v1/devices/enrollment-token",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={},
        )
        if token_resp.status_code != 200:
            logger.warning("sim_enroll_token_failed status=%s body=%s", token_resp.status_code, token_resp.text[:200])
            return
        token_data = token_resp.json()
        enroll_token = token_data["enrollment_token"]

        profile = dict(self.profile)
        profile["android_version"] = "14"
        profile["architecture"] = "arm64-v8a"
        enroll_resp = await client.post(
            f"{self.api_base}/api/v1/devices/enroll",
            headers={"Authorization": f"Bearer {self.admin_token}", "Content-Type": "application/json"},
            json={
                "enrollment_token": enroll_token,
                "device_public_key": f"sim-key-{uuid4().hex}",
                "hardware_fingerprint": f"sha256:{uuid4().hex}",
                "profile": profile,
            },
        )
        if enroll_resp.status_code == 200:
            data = enroll_resp.json()
            self.device_id = data.get("device_id")
            self.node_token = data.get("node_token")
            logger.info("sim_device_enrolled device_id=%s human_id=%s", self.device_id, data.get("human_id"))
        else:
            logger.warning("sim_enroll_failed status=%s body=%s", enroll_resp.status_code, enroll_resp.text[:200])

    async def _heartbeat(self, client: httpx.AsyncClient):
        if not self.device_id:
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
        }
        resp = await client.post(
            f"{self.api_base}/api/v1/devices/{self.device_id}/heartbeat",
            headers={"Authorization": f"Bearer {self.admin_token}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            logger.debug("sim_heartbeat_failed status=%s body=%s", resp.status_code, resp.text[:200])


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_base = os.environ.get("FARMOS_API_URL", "http://localhost:8000")
    admin_email = os.environ.get("FARMOS_SIM_ADMIN_EMAIL", "admin@example.com")
    admin_password = os.environ.get("FARMOS_SIM_ADMIN_PASSWORD", "ChangeMe123!")
    # Log in as the seeded admin so the simulator can issue enrollment tokens.
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
