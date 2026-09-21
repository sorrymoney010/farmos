"""FARMOS Device Agent — runs on a device, polls for jobs, executes, submits results."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.services.job_executor import execute_job

logger = logging.getLogger(__name__)


def sign_payload(private_key: ec.EllipticCurvePrivateKey, payload: dict[str, Any]) -> str:
    """Sign a payload with the device's private key."""
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


class FarmosDeviceAgent:
    """Device agent that enrolls, heartbeats, polls for jobs, executes them, and reports results."""

    def __init__(
        self,
        api_base: str,
        node_token: str | None = None,
        device_id: str | None = None,
        private_key: ec.EllipticCurvePrivateKey | None = None,
        public_key_pem: str | None = None,
        human_id: str | None = None,
        device_profile: dict[str, Any] | None = None,
    ):
        self.api_base = api_base.rstrip("/")
        self.node_token = node_token
        self.device_id = device_id
        self.human_id = human_id or f"FARM-NODE-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        self.private_key = private_key or ec.generate_private_key(ec.SECP256R1())
        self.public_key_pem = public_key_pem or self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        self.device_profile = device_profile or {
            "manufacturer": "Generic",
            "model": "FARMOS-Node",
            "cpu_cores": 4,
            "ram_mb": 2048,
            "storage_total_mb": 16000,
            "android_version": "14",
            "architecture": "arm64-v8a",
            "capabilities": ["cpu_compute", "network"],
        }
        self.running = False
        self.heartbeat_interval = int(os.environ.get("FARMOS_HEARTBEAT_INTERVAL", "30"))
        self.poll_interval = int(os.environ.get("FARMOS_JOB_POLL_INTERVAL", "10"))
        self.max_runtime = int(os.environ.get("FARMOS_MAX_JOB_RUNTIME", "120"))

    # ── Enrollment ───────────────────────────────────────────────────────────

    async def enroll(self, client: httpx.AsyncClient, enrollment_token: str) -> bool:
        """Enroll the device with FARMOS using an enrollment token."""
        profile = dict(self.device_profile)
        payload = {
            "enrollment_token": enrollment_token,
            "device_public_key": self.public_key_pem,
            "hardware_fingerprint": f"sha256:{uuid4().hex}",
            "profile": profile,
            "request_nonce": uuid4().hex,
            "request_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload["signature"] = sign_payload(self.private_key, payload)

        resp = await client.post(
            f"{self.api_base}/api/v1/devices/enroll",
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code == 200:
            data = resp.json()
            self.device_id = data.get("device_id")
            self.node_token = data.get("node_token")
            self.human_id = data.get("human_id", self.human_id)
            logger.info("device_enrolled", device_id=self.device_id, human_id=self.human_id)
            return True
        else:
            logger.warning("enroll_failed", status=resp.status_code, body=resp.text[:200])
            return False

    async def enroll_with_token(self, enrollment_token: str) -> bool:
        """Enroll using an enrollment token (convenience method)."""
        async with httpx.AsyncClient(timeout=15) as client:
            return await self.enroll(client, enrollment_token)

    # ── Heartbeat ─────────────────────────────────────────────────────────────


    async def enroll_with_claim_code(self, claim_code: str) -> bool:
        """Wireless onboarding: exchange short claim code → enrollment token → enroll.

        Staging may accept fixed code "123" when STAGING_CLAIM_CODES=true on the server.
        No USB required — only network reachability to the API.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.api_base}/api/v1/devices/claim-code/exchange",
                headers={"Content-Type": "application/json"},
                json={"claim_code": claim_code.strip()},
            )
            if resp.status_code != 200:
                logger.warning("claim_exchange_failed", status=resp.status_code, body=resp.text[:200])
                return False
            enrollment_token = resp.json().get("enrollment_token")
            if not enrollment_token:
                logger.warning("claim_exchange_missing_token", body=resp.text[:200])
                return False
            return await self.enroll(client, enrollment_token)


    async def heartbeat(self, client: httpx.AsyncClient) -> bool:
        """Send a heartbeat to FARMOS."""
        if not self.device_id or not self.node_token:
            return False

        observed_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "observed_at": observed_at,
            "battery_pct": 50.0,  # device should report real value
            "charging": True,
            "temperature_c": 35.0,
            "cpu_util_pct": 10.0,
            "ram_used_mb": 512,
            "storage_free_mb": 10000,
            "network": {"type": "wifi", "down_mbps": 100, "up_mbps": 50},
            "app_version": "1.0.0",
            "request_nonce": uuid4().hex,
            "request_timestamp": observed_at,
        }
        payload["signature"] = sign_payload(self.private_key, payload)

        resp = await client.post(
            f"{self.api_base}/api/v1/devices/{self.device_id}/heartbeat",
            headers={
                "Authorization": f"Bearer {self.node_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code == 200:
            logger.info("heartbeat_ok", device_id=self.device_id)
            return True
        else:
            logger.warning("heartbeat_failed", status=resp.status_code, body=resp.text[:200])
            return False

    async def heartbeat_once(self) -> bool:
        """Send one heartbeat."""
        async with httpx.AsyncClient(timeout=15) as client:
            return await self.heartbeat(client)

    # ── Job Polling ───────────────────────────────────────────────────────────

    async def poll_next_job(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        """Poll for the next dispatched job."""
        if not self.device_id or not self.node_token:
            return None

        resp = await client.post(
            f"{self.api_base}/api/v1/jobs/device/jobs/next",
            headers={
                "Authorization": f"Bearer {self.node_token}",
                "Content-Type": "application/json",
            },
            json={},
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("job"):
                logger.info("job_available", job_id=data["job"]["job_id"])
                return data
            else:
                logger.debug("no_job_available", device_id=self.device_id)
                return None
        else:
            logger.warning("poll_failed", status=resp.status_code, body=resp.text[:200])
            return None

    async def poll_next_job_once(self) -> dict[str, Any] | None:
        """Poll once for a job."""
        async with httpx.AsyncClient(timeout=15) as client:
            return await self.poll_next_job(client)

    # ── Job Acceptance ────────────────────────────────────────────────────────

    async def accept_job(self, client: httpx.AsyncClient, job_id: str) -> bool:
        """Accept a dispatched job."""
        if not self.device_id or not self.node_token:
            return False

        payload = {
            "request_nonce": uuid4().hex,
            "request_timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "device_id": self.device_id,
        }
        payload["signature"] = sign_payload(self.private_key, payload)

        resp = await client.post(
            f"{self.api_base}/api/v1/jobs/device/jobs/{job_id}/accept",
            headers={
                "Authorization": f"Bearer {self.node_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code == 200:
            logger.info("job_accepted", job_id=job_id)
            return True
        else:
            logger.warning("accept_failed", status=resp.status_code, body=resp.text[:200])
            return False

    async def accept_job_once(self, job_id: str) -> bool:
        """Accept a job (convenience method)."""
        async with httpx.AsyncClient(timeout=15) as client:
            return await self.accept_job(client, job_id)

    # ── Job Execution ─────────────────────────────────────────────────────────

    async def execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Execute a job and return the result."""
        workload_type = job.get("workload_type", "http_check")
        payload = job.get("payload", {})

        logger.info("executing_job", job_id=job.get("job_id"), type=workload_type)

        result = execute_job(
            None,  # db session not needed for local execution
            type("Job", (), {"workload_type": workload_type})(),
            self.device_id or "unknown",
            payload,
        )

        return result

    async def execute_job_once(self, job: dict[str, Any]) -> dict[str, Any]:
        """Execute a job (convenience method)."""
        return await self.execute_job(job)

    # ── Result Submission ─────────────────────────────────────────────────────

    async def submit_result(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        result: dict[str, Any],
    ) -> bool:
        """Submit a signed job result."""
        if not self.device_id or not self.node_token:
            return False

        result_hash = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        result_uri = json.dumps(result)

        payload = {
            "request_nonce": uuid4().hex,
            "request_timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "device_id": self.device_id,
            "result_hash": result_hash,
            "result_uri": result_uri,
            "execution_metrics": {
                "device_id": self.device_id,
                "device_human_id": self.human_id,
                "executed_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        payload["signature"] = sign_payload(self.private_key, payload)

        resp = await client.post(
            f"{self.api_base}/api/v1/jobs/device/jobs/{job_id}/result",
            headers={
                "Authorization": f"Bearer {self.node_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code == 200:
            logger.info("result_submitted", job_id=job_id, result_hash=result_hash)
            return True
        else:
            logger.warning("submit_failed", status=resp.status_code, body=resp.text[:200])
            return False

    async def submit_result_once(self, job_id: str, result: dict[str, Any]) -> bool:
        """Submit a result (convenience method)."""
        async with httpx.AsyncClient(timeout=15) as client:
            return await self.submit_result(client, job_id, result)

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self, *, once: bool = False):
        """Run the device agent loop."""
        logger.info(
            "agent_starting",
            device_id=self.device_id or "un enrolled",
            human_id=self.human_id,
            once=once,
        )

        async with httpx.AsyncClient(timeout=30) as client:
            # If not enrolled, try to enroll (for simulator/test use)
            if not self.device_id:
                logger.warning("agent_not_enrolled", "device must be enrolled first")
                return

            self.running = True
            while self.running:
                try:
                    # Heartbeat
                    hb_ok = await self.heartbeat(client)
                    if not hb_ok:
                        logger.warning("heartbeat_failed", device_id=self.device_id)

                    # Poll for job
                    job_data = await self.poll_next_job(client)
                    if job_data and job_data.get("job"):
                        job = job_data["job"]
                        job_id = job["job_id"]

                        # Accept job
                        accept_ok = await self.accept_job(client, job_id)
                        if not accept_ok:
                            logger.warning("job_not_accepted", job_id=job_id)
                            await self._sleep_with_backoff()
                            continue

                        # Execute job
                        result = await self.execute_job(job)
                        logger.info("job_executed", job_id=job_id, result_status=result.get("status"))

                        # Submit result
                        submit_ok = await self.submit_result(client, job_id, result)
                        if not submit_ok:
                            logger.warning("result_not_submitted", job_id=job_id)

                    # Wait before next poll
                    await self._sleep_with_backoff()

                    if once:
                        break

                except Exception as exc:
                    logger.error("agent_loop_error", error=str(exc))
                    await self._sleep_with_backoff()

        logger.info("agent_stopping", device_id=self.device_id)

    async def run_once(self):
        """Run one cycle: heartbeat → poll → execute → submit."""
        await self.run(once=True)

    async def _sleep_with_backoff(self):
        """Sleep with jitter to avoid thundering herd."""
        base = self.poll_interval if self.device_id else 5
        jitter = random.uniform(0, base * 0.5)
        await asyncio.sleep(base + jitter)


# ── Convenience: run a single job cycle ───────────────────────────────────────

async def run_single_job_cycle(
    api_base: str,
    node_token: str,
    device_id: str,
    private_key_pem: str,
) -> dict[str, Any]:
    """Run one complete job cycle: poll → accept → execute → submit → return result."""
    import asyncio

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None
    )

    agent = FarmosDeviceAgent(
        api_base=api_base,
        node_token=node_token,
        device_id=device_id,
        private_key=private_key,
    )

    async with httpx.AsyncClient(timeout=30) as client:
        # Poll
        job_data = await agent.poll_next_job(client)
        if not job_data or not job_data.get("job"):
            return {"status": "no_job", "message": "No job available"}

        job = job_data["job"]
        job_id = job["job_id"]

        # Accept
        accept_ok = await agent.accept_job(client, job_id)
        if not accept_ok:
            return {"status": "accept_failed", "job_id": job_id}

        # Execute
        result = await agent.execute_job(job)

        # Submit
        submit_ok = await agent.submit_result(client, job_id, result)
        if not submit_ok:
            return {"status": "submit_failed", "job_id": job_id, "result": result}

        return {
            "status": "completed",
            "job_id": job_id,
            "result": result,
        }


# ── CLI entry point ────────────────────────────────────────────────────────────

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    api_base = os.environ.get("FARMOS_API_URL", "http://localhost:8000")
    node_token = os.environ.get("FARMOS_NODE_TOKEN", "")
    device_id = os.environ.get("FARMOS_DEVICE_ID", "")
    private_key_pem = os.environ.get("FARMOS_PRIVATE_KEY_PEM", "")

    once = os.environ.get("FARMOS_AGENT_ONCE", "").lower() in {"1", "true", "yes"}
    poll_only = os.environ.get("FARMOS_AGENT_POLL_ONLY", "").lower() in {"1", "true", "yes"}

    claim_code = os.environ.get("FARMOS_CLAIM_CODE", "").strip()
    if claim_code and (not node_token or not device_id):
        agent = FarmosDeviceAgent(api_base=api_base)
        ok = await agent.enroll_with_claim_code(claim_code)
        if not ok:
            logger.error("claim_enroll_failed", claim_code=claim_code)
            return
        node_token = agent.node_token or ""
        device_id = agent.device_id or ""
        logger.info("claim_enroll_ok", device_id=device_id)

    if not node_token or not device_id:
        logger.error(
            "agent_missing_credentials",
            node_token=bool(node_token),
            device_id=bool(device_id),
            hint="Set FARMOS_NODE_TOKEN and FARMOS_DEVICE_ID env vars",
        )
        return

    agent = FarmosDeviceAgent(
        api_base=api_base,
        node_token=node_token,
        device_id=device_id,
        private_key_pem=private_key_pem or "",
    )

    if once:
        await agent.run_once()
    else:
        agent.running = True
        try:
            await agent.run()
        except KeyboardInterrupt:
            agent.running = False
            logger.info("agent_stopped_by_user")


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass