"""FARMOS job execution and verification service."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import JobStatus, JobType, LedgerAccountType, LedgerDirection, DeviceStatus
from app.db.models.jobs_ledger import Job, JobAssignment, JobResult, JobVerification, JobEvent, LedgerAccount, LedgerEntry
from app.db.models.devices import Device
from app.logging import get_logger

logger = get_logger(__name__)

LEDGER_CONFIG: dict[JobType, dict[str, float]] = {
    JobType.HTTP_CHECK: {"reward_test_usd": 0.0001, "reward_real_usd": 0.0025},
    JobType.API_REQUEST: {"reward_test_usd": 0.0001, "reward_real_usd": 0.005},
    JobType.MONITOR: {"reward_test_usd": 0.0001, "reward_real_usd": 0.001},
    JobType.DATA_TRANSFORM: {"reward_test_usd": 0.0001, "reward_real_usd": 0.003},
}


async def ensure_device_earnings_accounts(db: AsyncSession, device_id: UUID) -> tuple[UUID, UUID]:
    """Ensure a device has both test and real earnings ledger accounts.

    Returns (test_account_id, real_account_id).
    """
    test_account = await db.execute(
        select(LedgerAccount).where(
            LedgerAccount.owner_type == "DEVICE",
            LedgerAccount.owner_id == device_id,
            LedgerAccount.account_type == LedgerAccountType.DEVICE_EARNINGS_TEST,
            LedgerAccount.asset == "USD",
        )
    )
    test_account = test_account.scalar_one_or_none()
    if not test_account:
        test_account = LedgerAccount(
            owner_type="DEVICE",
            owner_id=device_id,
            account_type=LedgerAccountType.DEVICE_EARNINGS_TEST,
            asset="USD",
        )
        db.add(test_account)
        await db.flush()

    real_account = await db.execute(
        select(LedgerAccount).where(
            LedgerAccount.owner_type == "DEVICE",
            LedgerAccount.owner_id == device_id,
            LedgerAccount.account_type == LedgerAccountType.DEVICE_EARNINGS_REAL,
            LedgerAccount.asset == "USDC",
        )
    )
    real_account = real_account.scalar_one_or_none()
    if not real_account:
        real_account = LedgerAccount(
            owner_type="DEVICE",
            owner_id=device_id,
            account_type=LedgerAccountType.DEVICE_EARNINGS_REAL,
            asset="USDC",
        )
        db.add(real_account)
        await db.flush()

    return test_account.id, real_account.id


async def execute_job(db: AsyncSession, job: Job, device_id: UUID, result_payload: dict[str, Any]) -> dict[str, Any]:
    """Execute a job on behalf of a device and return execution metrics."""
    job_type = JobType(job.workload_type)
    config = LEDGER_CONFIG.get(job_type, {"reward_test_usd": 0.0001, "reward_real_usd": 0.001})

    if job_type == JobType.HTTP_CHECK:
        return _execute_http_check(result_payload, config)
    elif job_type == JobType.API_REQUEST:
        return _execute_api_request(result_payload, config)
    elif job_type == JobType.MONITOR:
        return _execute_monitor(result_payload, config)
    elif job_type == JobType.DATA_TRANSFORM:
        return _execute_data_transform(result_payload, config)
    else:
        return {
            "status": "FAILED",
            "error": f"Unknown job type: {job.workload_type}",
            "duration_ms": 0,
            "bytes_transferred": 0,
        }


def _execute_http_check(payload: dict[str, Any], config: dict[str, float]) -> dict[str, Any]:
    """Execute an HTTP check job."""
    import httpx

    url_raw = payload.get("url")
    if not url_raw or not isinstance(url_raw, str):
        url_raw = "https://example.com"
    url: str = url_raw
    method: str = str(payload.get("method", "GET")).upper()
    timeout: float = float(payload.get("timeout", 30))
    headers: dict[str, str] = {str(k): str(v) for k, v in (payload.get("headers") or {}).items()}

    try:
        async def _run() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=timeout) as client:
                start = datetime.now(timezone.utc)
                resp = await client.request(method, url, headers=headers)
                duration_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
                body = resp.text[:1024] if resp.text else ""
                return {
                    "status": "COMPLETED",
                    "http_status": resp.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "bytes_transferred": len(body.encode()),
                    "response_preview": body,
                    "url": url,
                    "method": method,
                }

        import asyncio
        return asyncio.run(_run())
    except Exception as exc:
        return {
            "status": "FAILED",
            "error": str(exc),
            "duration_ms": 0,
            "bytes_transferred": 0,
            "url": url,
            "method": method,
        }


def _execute_api_request(payload: dict[str, Any], config: dict[str, float]) -> dict[str, Any]:
    """Execute an API request job."""
    import httpx

    url_raw = payload.get("url")
    if not url_raw or not isinstance(url_raw, str):
        return {"status": "FAILED", "error": "Missing required 'url' in payload", "duration_ms": 0, "bytes_transferred": 0}
    url: str = url_raw
    method: str = str(payload.get("method", "GET")).upper()
    body: Any = payload.get("body")
    headers: dict[str, str] = {str(k): str(v) for k, v in (payload.get("headers") or {}).items()}
    timeout: float = float(payload.get("timeout", 30))

    try:
        async def _run() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=timeout) as client:
                start = datetime.now(timezone.utc)
                resp = await client.request(method, url, json=body, headers=headers)
                duration_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
                return {
                    "status": "COMPLETED",
                    "http_status": resp.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "bytes_transferred": len(resp.content),
                    "url": url,
                    "method": method,
                }

        import asyncio
        return asyncio.run(_run())
    except Exception as exc:
        return {
            "status": "FAILED",
            "error": str(exc),
            "duration_ms": 0,
            "bytes_transferred": 0,
        }


def _execute_monitor(payload: dict[str, Any], config: dict[str, float]) -> dict[str, Any]:
    """Execute a monitoring job (uptime, response time, etc.)."""
    import httpx

    targets_raw = payload.get("targets", [])
    targets: list[str] = [str(t) for t in targets_raw if isinstance(t, (str, int, float))]
    results: list[dict[str, Any]] = []

    async def _run() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            for target in targets:
                try:
                    start = datetime.now(timezone.utc)
                    resp = await client.get(target, timeout=10)
                    duration_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
                    results.append({
                        "url": target,
                        "status": "UP",
                        "http_status": resp.status_code,
                        "response_time_ms": round(duration_ms, 2),
                    })
                except Exception as exc:
                    results.append({
                        "url": target,
                        "status": "DOWN",
                        "error": str(exc),
                    })
        return {
            "status": "COMPLETED",
            "targets_checked": len(targets),
            "targets_up": sum(1 for r in results if r["status"] == "UP"),
            "targets_down": sum(1 for r in results if r["status"] == "DOWN"),
            "details": results,
        }

    import asyncio
    return asyncio.run(_run())


def _execute_data_transform(payload: dict[str, Any], config: dict[str, float]) -> dict[str, Any]:
    """Execute a data transformation job."""
    data: list[Any] = payload.get("data", [])
    transform: str = str(payload.get("transform", "identity"))

    try:
        if transform == "identity":
            result: Any = data
        elif transform == "uppercase":
            result = [str(item).upper() for item in data]
        elif transform == "lowercase":
            result = [str(item).lower() for item in data]
        elif transform == "sum":
            result = sum(float(item) for item in data)
        else:
            result = data

        return {
            "status": "COMPLETED",
            "transform": transform,
            "input_count": len(data),
            "output": result,
        }
    except Exception as exc:
        return {
            "status": "FAILED",
            "error": str(exc),
            "transform": transform,
        }


async def verify_result(db: AsyncSession, job: Job, result: JobResult) -> bool:
    """Verify a job result. Returns True if valid."""
    del db  # unused, kept for signature compatibility
    job_type = JobType(job.workload_type)

    # Parse result payload from result_hash (hex) or result_uri (JSON string)
    result_payload: dict[str, Any]
    if result.result_uri:
        try:
            result_payload = json.loads(result.result_uri) if isinstance(result.result_uri, str) else dict(result.result_uri)
        except (json.JSONDecodeError, TypeError, ValueError):
            result_payload = {"result_hash": result.result_hash, "raw": str(result.result_uri)}
    else:
        result_payload = {"result_hash": result.result_hash}

    if job_type == JobType.HTTP_CHECK:
        return _verify_http_check(result_payload)
    elif job_type == JobType.API_REQUEST:
        return _verify_api_request(result_payload)
    elif job_type == JobType.MONITOR:
        return _verify_monitor(result_payload)
    elif job_type == JobType.DATA_TRANSFORM:
        return _verify_data_transform(result_payload)
    return False


def _verify_http_check(result_data: dict[str, Any]) -> bool:
    """Verify an HTTP check result."""
    if result_data.get("status") != "COMPLETED":
        return False
    http_status = result_data.get("http_status")
    return http_status is not None and isinstance(http_status, int) and 100 <= http_status < 600


def _verify_api_request(result_data: dict[str, Any]) -> bool:
    """Verify an API request result."""
    if result_data.get("status") != "COMPLETED":
        return False
    http_status = result_data.get("http_status")
    return http_status is not None and isinstance(http_status, int) and 100 <= http_status < 600


def _verify_monitor(result_data: dict[str, Any]) -> bool:
    """Verify a monitor result."""
    if result_data.get("status") != "COMPLETED":
        return False
    targets_checked = result_data.get("targets_checked")
    return targets_checked is not None and isinstance(targets_checked, int) and targets_checked > 0


def _verify_data_transform(result_data: dict[str, Any]) -> bool:
    """Verify a data transform result."""
    if result_data.get("status") != "COMPLETED":
        return False
    return "output" in result_data


async def credit_device_earnings(
    db: AsyncSession,
    job: Job,
    device_id: UUID,
    result: JobResult,
    is_real_revenue: bool = False,
) -> LedgerEntry:
    """Credit the device's earnings ledger for a verified job result.

    - If is_real_revenue=True: credit DEVICE_EARNINGS_REAL (USDC)
    - If is_real_revenue=False: credit DEVICE_EARNINGS_TEST (USD) — test credits only
    """
    job_type = JobType(job.workload_type)
    config = LEDGER_CONFIG.get(job_type, {"reward_test_usd": 0.0001, "reward_real_usd": 0.001})

    if is_real_revenue:
        amount = config["reward_real_usd"]
        asset = "USDC"
        account_type = LedgerAccountType.DEVICE_EARNINGS_REAL
        reference_type = "JOB_RESULT_REAL"
    else:
        amount = config["reward_test_usd"]
        asset = "USD"
        account_type = LedgerAccountType.DEVICE_EARNINGS_TEST
        reference_type = "JOB_RESULT_TEST"

    test_account_id, real_account_id = await ensure_device_earnings_accounts(db, device_id)
    account_id: UUID = real_account_id if is_real_revenue else test_account_id

    transaction_id: UUID = uuid4()
    entry = LedgerEntry(
        transaction_id=transaction_id,
        account_id=account_id,
        direction=LedgerDirection.CREDIT,
        amount=amount,
        asset=asset,
        usd_value=amount,
        reference_type=reference_type,
        reference_id=job.id,
        event_metadata={
            "job_id": str(job.id),
            "device_id": str(device_id),
            "job_type": job.workload_type,
            "result_id": str(result.id),
            "is_real_revenue": is_real_revenue,
        },
    )
    db.add(entry)
    await db.flush()
    return entry
