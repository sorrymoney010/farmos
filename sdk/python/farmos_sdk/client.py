"""Thin, dependency-light client for the FARMOS developer API.

    from farmos_sdk import FarmosClient
    fos = FarmosClient("https://farmos.example.com", api_key="fos_test_...")
    q = fos.quote("http_check", {"url": "https://example.com"})
    dep = fos.create_deployment("http_check", {"url": "https://example.com"},
                                schedule={"interval_seconds": 300, "max_runs": 288},
                                quote_id=q["quote_id"])
    for run in fos.runs(dep["deployment_id"]):
        print(run["run_index"], run["job_status"], run["result"])

The API key is read from the argument or the FARMOS_API_KEY environment
variable; it is never written anywhere by the SDK.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from typing import Any, Optional

import httpx

API_PREFIX = "/api/v1/developer"


class FarmosError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code, self.detail = status_code, detail


class FarmosClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, *,
                 token: Optional[str] = None, http: Optional[httpx.Client] = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get("FARMOS_API_URL") or "http://localhost:8000").rstrip("/")
        self.api_key = api_key or os.environ.get("FARMOS_API_KEY")
        self.token = token  # dashboard JWT (key management only)
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._prefix = API_PREFIX

    # ── plumbing ──
    def _headers(self, idempotency_key: Optional[str] = None, jwt: bool = False) -> dict[str, str]:
        h = {"User-Agent": "farmos-sdk/0.1"}
        cred = self.token if jwt else (self.api_key or self.token)
        if cred:
            h["Authorization"] = f"Bearer {cred}"
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _req(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
             idempotency_key: Optional[str] = None, jwt: bool = False) -> Any:
        r = self._http.request(method, f"{self._prefix}{path}", json=json, params=params,
                               headers=self._headers(idempotency_key, jwt))
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise FarmosError(r.status_code, body.get("detail", body) if isinstance(body, dict) else body)
        return body

    # ── account / keys (keys need a dashboard JWT) ──
    def account(self) -> dict:
        return self._req("GET", "/account")

    def create_key(self, name: str, scopes: Optional[list[str]] = None, expires_in_days: Optional[int] = None) -> dict:
        return self._req("POST", "/keys", json={"name": name, "scopes": scopes, "expires_in_days": expires_in_days},
                         jwt=True)

    def list_keys(self) -> list:
        return self._req("GET", "/keys", jwt=True)

    def revoke_key(self, key_id: str) -> dict:
        return self._req("DELETE", f"/keys/{key_id}", jwt=True)

    # ── pricing / deployments ──
    def quote(self, workload_type: str, payload: dict, resources: Optional[dict] = None, priority: int = 100,
              runs: Optional[int] = None) -> dict:
        return self._req("POST", "/quotes", json={"workload_type": workload_type, "payload": payload,
                                                  "resources": resources or {}, "priority": priority, "runs": runs})

    def create_deployment(self, workload_type: str, payload: dict, *, name: Optional[str] = None,
                          resources: Optional[dict] = None, schedule: Optional[dict] = None,
                          destination: Optional[dict] = None, priority: int = 100, quote_id: Optional[str] = None,
                          max_price_usd: Optional[float] = None, idempotency_key: Optional[str] = None) -> dict:
        body = {"name": name, "workload_type": workload_type, "payload": payload, "resources": resources or {},
                "schedule": schedule or {}, "destination": destination or {"type": "api"}, "priority": priority,
                "quote_id": quote_id, "max_price_usd": max_price_usd}
        return self._req("POST", "/deployments", json=body, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def deployments(self, status: Optional[str] = None) -> list:
        return self._req("GET", "/deployments", params={"status": status} if status else None)

    def deployment(self, deployment_id: str) -> dict:
        return self._req("GET", f"/deployments/{deployment_id}")

    def cancel_deployment(self, deployment_id: str) -> dict:
        return self._req("POST", f"/deployments/{deployment_id}/cancel")

    def runs(self, deployment_id: str, limit: int = 50, offset: int = 0) -> list:
        return self._req("GET", f"/deployments/{deployment_id}/runs",
                         params={"limit": limit, "offset": offset})["runs"]

    def job(self, job_id: str) -> dict:
        return self._req("GET", f"/jobs/{job_id}")

    # ── billing ──
    def balance(self) -> dict:
        return self._req("GET", "/balance")

    def ledger(self, limit: int = 100) -> list:
        return self._req("GET", "/ledger", params={"limit": limit})

    def topup(self, amount_usd: float, method: str = "checkout", idempotency_key: Optional[str] = None) -> dict:
        return self._req("POST", "/topups", json={"amount_usd": amount_usd, "method": method},
                         idempotency_key=idempotency_key or uuid.uuid4().hex)

    def topups(self) -> list:
        return self._req("GET", "/topups")


def verify_webhook_signature(secret: str, body: bytes, header: str, tolerance: int = 300,
                             now: Optional[float] = None) -> bool:
    """Verify a ``FARMOS-Signature: t=<ts>,v1=<hex>`` header on a deployment webhook."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))
