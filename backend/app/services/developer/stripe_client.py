"""Minimal Stripe REST client + webhook signature verification (spec §101, §104).

* Credentials come only from the environment (``STRIPE_SECRET_KEY``,
  ``STRIPE_WEBHOOK_SECRET``); nothing is committed or generated.
* ``sk_test_`` / ``rk_test_`` keys run in TEST mode and post to the TEST book.
  Live keys are refused unless ``STRIPE_LIVE_MODE_ENABLED=true`` AND
  ``APP_ENV=production`` (the owner's explicit go-live switch); live funding
  then posts to the REAL book.
* No SDK dependency: Checkout Sessions / PaymentIntents are created with form
  encoded POSTs; tests inject an ``httpx.MockTransport`` via ``TRANSPORT`` so no
  request ever leaves the process.
* Webhook verification follows Stripe's scheme: ``Stripe-Signature:
  t=<ts>,v1=<hex>`` where v1 = HMAC-SHA256(secret, f"{t}.{raw_body}"), with a
  timestamp tolerance against replay.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.finance import ledger

TRANSPORT: Optional[httpx.AsyncBaseTransport] = None  # tests: httpx.MockTransport


class StripeError(Exception):
    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


class SignatureError(Exception):
    pass


def mode() -> str:
    """'test' | 'live' | 'unconfigured' | 'live_disabled'."""
    key = settings.STRIPE_SECRET_KEY or ""
    if not key:
        return "unconfigured"
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    if key.startswith(("sk_live_", "rk_live_")):
        if settings.STRIPE_LIVE_MODE_ENABLED and settings.APP_ENV == "production":
            return "live"
        return "live_disabled"
    return "unconfigured"


def book() -> str:
    """Ledger book customer funding/charges post to."""
    return ledger.REAL if mode() == "live" else ledger.TEST


def status() -> dict[str, Any]:
    m = mode()
    return {"mode": m, "book": book(), "checkout_enabled": m in ("test", "live"),
            "webhook_secret_configured": bool(settings.STRIPE_WEBHOOK_SECRET),
            "note": {"unconfigured": "set STRIPE_SECRET_KEY (sk_test_...) to enable top-ups",
                     "live_disabled": "live key present but live mode is disabled; refusing to charge real cards",
                     "test": "Stripe TEST mode: no real money moves",
                     "live": "Stripe LIVE mode"}[m]}


def require_enabled() -> None:
    m = mode()
    if m == "unconfigured":
        raise StripeError("Stripe is not configured (STRIPE_SECRET_KEY)", 503)
    if m == "live_disabled":
        raise StripeError("live Stripe key refused: live mode is disabled in this deployment", 503)


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out.extend(_flatten(v, key))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.extend(_flatten(item, f"{key}[{i}]"))
                else:
                    out.append((f"{key}[{i}]", str(item)))
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        elif v is not None:
            out.append((key, str(v)))
    return out


async def _post(path: str, data: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    require_enabled()
    async with httpx.AsyncClient(base_url=settings.STRIPE_API_BASE, transport=TRANSPORT,
                                 timeout=settings.STRIPE_HTTP_TIMEOUT_SECONDS) as c:
        try:
            r = await c.post(path, content=urlencode(_flatten(data)).encode(),
                             headers={"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
                                      "Content-Type": "application/x-www-form-urlencoded",
                                      "Idempotency-Key": idempotency_key,
                                      "Stripe-Version": "2024-06-20"})
        except httpx.HTTPError as exc:
            raise StripeError(f"Stripe unreachable: {type(exc).__name__}") from exc
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400:
        msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
        raise StripeError(f"Stripe error {r.status_code}: {msg or 'request failed'}")
    return body


async def create_checkout_session(*, topup_id: str, customer_id: str, amount_cents: int,
                                  email: Optional[str]) -> dict[str, Any]:
    meta = {"farmos_topup_id": topup_id, "farmos_customer_id": customer_id}
    data = {
        "mode": "payment",
        "client_reference_id": topup_id,
        "success_url": settings.STRIPE_CHECKOUT_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CHECKOUT_CANCEL_URL,
        "line_items": [{"quantity": 1, "price_data": {
            "currency": "usd", "unit_amount": amount_cents,
            "product_data": {"name": "FARMOS prepaid compute credit"}}}],
        "metadata": meta,
        "payment_intent_data": {"metadata": meta},
        "customer_email": email,
    }
    return await _post("/v1/checkout/sessions", data, idempotency_key=f"farmos-topup-{topup_id}")


async def create_payment_intent(*, topup_id: str, customer_id: str, amount_cents: int) -> dict[str, Any]:
    data = {"amount": amount_cents, "currency": "usd", "automatic_payment_methods": {"enabled": True},
            "metadata": {"farmos_topup_id": topup_id, "farmos_customer_id": customer_id}}
    return await _post("/v1/payment_intents", data, idempotency_key=f"farmos-topup-{topup_id}")


def sign_payload(payload: bytes, secret: str, timestamp: Optional[int] = None) -> str:
    """Build a Stripe-Signature header (used by tests and local tooling)."""
    t = int(timestamp if timestamp is not None else time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def verify_and_parse(payload: bytes, header: Optional[str], secret: Optional[str] = None,
                     tolerance: Optional[int] = None, now: Optional[float] = None) -> dict[str, Any]:
    secret = secret if secret is not None else settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        raise SignatureError("webhook secret not configured")
    if not header:
        raise SignatureError("missing Stripe-Signature header")
    ts, sigs = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sigs.append(v)
    if ts is None or not ts.isdigit() or not sigs:
        raise SignatureError("malformed Stripe-Signature header")
    tol = settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS if tolerance is None else tolerance
    if abs((now if now is not None else time.time()) - int(ts)) > tol:
        raise SignatureError("timestamp outside tolerance (possible replay)")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("signature mismatch")
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise SignatureError("payload is not JSON") from exc
    if not isinstance(event, dict) or "id" not in event or "type" not in event:
        raise SignatureError("payload is not a Stripe event")
    return event


async def retrieve_payment_evidence(payment_intent_id: str) -> dict[str, Any]:
    """Read only, fixed Stripe origin; never return client secrets to API callers."""
    import re
    if not re.fullmatch(r"pi_[A-Za-z0-9]+", payment_intent_id):
        raise StripeError("invalid stored payment intent identifier", 400)
    require_enabled()
    async with httpx.AsyncClient(base_url="https://api.stripe.com", transport=TRANSPORT,
                                 trust_env=False, follow_redirects=False,
                                 timeout=settings.STRIPE_HTTP_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"/v1/payment_intents/{payment_intent_id}",
                params={"expand[]": "latest_charge.balance_transaction"},
                headers={"Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
                         "Stripe-Version": "2024-06-20"})
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Do not expose processor responses, credentials or payment secrets.
            raise StripeError("Stripe evidence retrieval failed") from exc
    if not isinstance(data, dict):
        raise StripeError("invalid Stripe evidence response")
    return data
