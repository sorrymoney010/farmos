"""Device-side workload executors for the Python node agent.

Fully async (safe inside the agent's event loop). CPU/data workloads run in a
worker thread via ``asyncio.to_thread`` and share their implementation with the
server-side verifier (``app.workloads.cpu_data``), so honest results always
verify. Results avoid floats where verification depends on them.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.workloads import cpu_data

HTTP_PREVIEW_BYTES = 1024
SUPPORTED = {"http_check", "api_request", "monitor", *cpu_data.CPU_DATA_WORKLOADS}


async def _http(payload: dict[str, Any], *, require_url: bool) -> dict[str, Any]:
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        if require_url or url is not None:
            return {"status": "FAILED", "error": "missing or invalid url", "duration_ms": 0, "bytes_transferred": 0}
        url = "https://example.com"
    method = str(payload.get("method", "GET")).upper()
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
        return {"status": "FAILED", "error": f"unsupported method {method}", "duration_ms": 0, "bytes_transferred": 0}
    timeout = min(float(payload.get("timeout", 30)), 60.0)
    headers = {str(k): str(v) for k, v in (payload.get("headers") or {}).items()}
    body = payload.get("body")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(method, url, headers=headers,
                                        json=body if isinstance(body, (dict, list)) else None)
        text = resp.text or ""
        preview = text.encode("utf-8")[:HTTP_PREVIEW_BYTES].decode("utf-8", "ignore")
        return {
            "status": "COMPLETED",
            "http_status": resp.status_code,
            "duration_ms": int((time.monotonic() - start) * 1000),
            "bytes_transferred": len(resp.content or b""),
            "response_preview": preview,
            "url": url,
            "method": method,
        }
    except Exception as exc:  # network errors are reported, not raised
        return {"status": "FAILED", "error": str(exc)[:500], "duration_ms": int((time.monotonic() - start) * 1000),
                "bytes_transferred": 0, "url": url, "method": method}


async def _monitor(payload: dict[str, Any]) -> dict[str, Any]:
    targets = [t for t in (payload.get("targets") or []) if isinstance(t, str)][:20]
    details = []
    for t in targets:
        r = await _http({"url": t, "method": "GET", "timeout": 10}, require_url=True)
        details.append({"url": t, "status": "UP" if r["status"] == "COMPLETED" else "DOWN",
                        "http_status": r.get("http_status"), "response_time_ms": r.get("duration_ms")})
    return {"status": "COMPLETED", "targets_checked": len(targets),
            "targets_up": sum(1 for d in details if d["status"] == "UP"),
            "targets_down": sum(1 for d in details if d["status"] == "DOWN"), "details": details}


async def run_workload(workload_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    if workload_type in cpu_data.CPU_DATA_WORKLOADS:
        start = time.monotonic()
        result = await asyncio.to_thread(cpu_data.execute, workload_type, payload)
        # duration is informational only (not verified); keep it an int
        result["duration_ms"] = int((time.monotonic() - start) * 1000)
        return result
    if workload_type == "http_check":
        return await _http(payload, require_url=False)
    if workload_type == "api_request":
        return await _http(payload, require_url=True)
    if workload_type == "monitor":
        return await _monitor(payload)
    return {"status": "FAILED", "error": f"unsupported workload {workload_type}"}


def result_hash_and_uri(result: dict[str, Any]) -> tuple[str, str]:
    """Canonical (normalized-number) JSON is both the signed result_uri and the hash input."""
    uri = cpu_data.canonical(result)
    return cpu_data.sha256_hex(uri), uri
