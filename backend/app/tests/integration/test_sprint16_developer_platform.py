"""Sprint 16 integration: FARMOS developer platform (own task supply).

Stripe is fully mocked: outbound REST calls go to an ``httpx.MockTransport``
and webhooks are signed in-process with a throwaway test secret. No network,
no Stripe account, no real keys. All money lands on the TEST book.
"""

from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.services.developer import deployments as dep_svc
from app.services.developer import stripe_client
from app.tests.integration.test_sprint3_benchmarks_safety import BASE, admin, client  # noqa: F401
from app.tests.integration.test_sprint4_5_leases_verification import (  # noqa: F401
    _isolate, _no_random_redundancy, _ready_node, _sql,
)
from app.tests.integration.test_sprint6_7_opportunities_scheduler import _complete, _tick
from app.tests.integration.test_sprint8_9_ledger_wallets_withdrawals import _switches_off, _user  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "sdk" / "python"))
from farmos_sdk import FarmosClient, FarmosError, verify_webhook_signature  # noqa: E402
from farmos_sdk.cli import run as cli_run  # noqa: E402

DEV = f"{BASE}/developer"
WHSEC = "whsec_unit_test_only_not_a_real_secret"
FAKE_SK = "sk_test_" + "0" * 24  # obviously fake; the transport is mocked
CHK = {"seed": "farm", "size_bytes": 65536, "algorithm": "sha256"}


class FakeStripe:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(402, json={"error": {"message": "card declined (mock)"}})
        form = parse_qs(request.content.decode())
        if request.url.path == "/v1/checkout/sessions":
            sid = f"cs_test_{uuid4().hex[:20]}"
            return httpx.Response(200, json={"id": sid, "object": "checkout.session",
                                             "url": f"https://checkout.stripe.com/c/pay/{sid}",
                                             "amount_total": int(form["line_items[0][price_data][unit_amount]"][0]),
                                             "payment_intent": None})
        if request.url.path == "/v1/payment_intents":
            pid = f"pi_test_{uuid4().hex[:20]}"
            return httpx.Response(200, json={"id": pid, "object": "payment_intent",
                                             "client_secret": f"{pid}_secret_mock", "amount": int(form["amount"][0])})
        return httpx.Response(404, json={"error": {"message": "unknown path"}})


@pytest.fixture
def stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", FAKE_SK)
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", WHSEC)
    monkeypatch.setattr(settings, "STRIPE_LIVE_MODE_ENABLED", False)
    monkeypatch.setattr(stripe_client, "TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def hooks(monkeypatch):
    received: list[httpx.Request] = []
    status = {"code": 200}

    def handler(request):
        received.append(request)
        return httpx.Response(status["code"], json={"ok": True})
    monkeypatch.setattr(dep_svc, "TRANSPORT", httpx.MockTransport(handler))
    return received, status


# ── helpers ──────────────────────────────────────────────────────────────────

def _dev(client):
    h, uid = _user(client, "dev")
    r = client.post(f"{DEV}/account", headers=h, json={"name": "Acme Monitoring"})
    assert r.status_code == 200, r.text
    return h, r.json()["customer_id"]


def _key(client, h, scopes=None, **kw):
    r = client.post(f"{DEV}/keys", headers=h, json={"name": "ci", "scopes": scopes, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _kh(key):
    return {"Authorization": f"Bearer {key['api_key']}"}


def _post_event(client, event: dict, *, secret=WHSEC, ts=None, header=None):
    raw = json.dumps(event).encode()
    sig = header if header is not None else stripe_client.sign_payload(raw, secret, ts)
    return client.post(f"{BASE}/webhooks/stripe", content=raw,
                       headers={"Stripe-Signature": sig, "Content-Type": "application/json"})


def _session_event(topup, *, amount=None, etype="checkout.session.completed", paid=True, livemode=False,
                   event_id=None, currency="usd"):
    return {"id": event_id or f"evt_{uuid4().hex[:24]}", "object": "event", "type": etype, "livemode": livemode,
            "created": int(time.time()),
            "data": {"object": {"id": topup["session_id"], "object": "checkout.session",
                                "client_reference_id": topup["topup_id"],
                                "amount_total": amount if amount is not None else topup["cents"],
                                "currency": currency, "payment_status": "paid" if paid else "unpaid",
                                "payment_intent": topup.get("pi") or f"pi_test_{topup['topup_id'][:8]}",
                                "metadata": {"farmos_topup_id": topup["topup_id"],
                                             "farmos_customer_id": topup["customer_id"]}}}}


def _topup(client, h, stripe, amount=20.0, customer_id=None):
    r = client.post(f"{DEV}/topups", headers={**h, "Idempotency-Key": uuid4().hex}, json={"amount_usd": amount})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "PENDING" and body["checkout_url"].startswith("https://checkout.stripe.com/")
    sid = _sql("SELECT stripe_checkout_session_id FROM customer_topups WHERE id = CAST(:t AS uuid)",
               t=body["topup_id"])[0][0]
    cid = customer_id or _sql("SELECT customer_id::text FROM customer_topups WHERE id = CAST(:t AS uuid)",
                              t=body["topup_id"])[0][0]
    return {"topup_id": body["topup_id"], "session_id": sid, "cents": int(round(amount * 100)), "customer_id": cid}


def _funded(client, stripe, amount=20.0):
    h, cid = _dev(client)
    t = _topup(client, h, stripe, amount, cid)
    r = _post_event(client, _session_event(t))
    assert r.status_code == 200 and r.json()["status"] == "PROCESSED", r.text
    return h, cid, t


def _bal(client, h):
    b = client.get(f"{DEV}/balance", headers=h).json()
    return {k: Decimal(b[k]) for k in ("balance_usd", "reserved_usd", "available_usd")}


def _quote(client, h, workload="checksum", payload=None, **kw):
    r = client.post(f"{DEV}/quotes", headers=h, json={"workload_type": workload, "payload": payload or CHK, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _deploy(client, h, workload="checksum", payload=None, expect=201, key=None, **kw):
    payload = payload if payload is not None else CHK
    if "quote_id" not in kw and "max_price_usd" not in kw:
        kw["quote_id"] = _quote(client, h, workload, payload,
                                **{k: kw[k] for k in ("resources", "priority") if k in kw})["quote_id"]
    r = client.post(f"{DEV}/deployments", headers={**h, "Idempotency-Key": key or uuid4().hex},
                    json={"workload_type": workload, "payload": payload, **kw})
    assert r.status_code == expect, r.text
    return r.json()


def _runs(client, h, dep_id):
    r = client.get(f"{DEV}/deployments/{dep_id}/runs", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["runs"]


def _admin_tick(client, admin):
    r = client.post(f"{BASE}/admin/developer/tick", headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


# ── accounts + keys ─────────────────────────────────────────────────────────

def test_account_keys_hashed_scoped_and_revocable(client):
    h, cid = _dev(client)
    assert client.post(f"{DEV}/account", headers=h, json={}).json()["created"] is False
    k = _key(client, h, scopes=["jobs:read"])
    assert k["api_key"].startswith("fos_test_") and k["prefix"] in k["api_key"]
    stored = _sql("SELECT secret_hash FROM customer_api_keys WHERE id = CAST(:k AS uuid)", k=k["key_id"])[0][0]
    assert k["api_key"] not in stored and len(stored) == 64            # only an HMAC is stored
    assert "api_key" not in json.dumps(client.get(f"{DEV}/keys", headers=h).json())  # never shown again

    kh = _kh(k)
    assert client.get(f"{DEV}/account", headers=kh).json()["customer_id"] == cid
    assert client.get(f"{DEV}/deployments", headers={"X-API-Key": k["api_key"]}).status_code == 200
    assert client.get(f"{DEV}/balance", headers=kh).status_code == 403          # billing:read missing
    assert client.post(f"{DEV}/deployments", headers=kh, json={"workload_type": "checksum", "payload": CHK,
                                                               "max_price_usd": 1}).status_code == 403
    assert client.post(f"{DEV}/keys", headers=kh, json={"name": "x"}).status_code == 401  # keys can't mint keys
    assert client.post(f"{DEV}/keys", headers=h, json={"name": "x", "scopes": ["admin:all"]}).status_code == 400

    tampered = k["api_key"][:-2] + ("aa" if not k["api_key"].endswith("aa") else "bb")
    assert client.get(f"{DEV}/account", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401
    assert client.delete(f"{DEV}/keys/{k['key_id']}", headers=h).json()["active"] is False
    assert client.get(f"{DEV}/account", headers=kh).status_code == 401           # revoked

    _sql("UPDATE customer_api_keys SET expires_at = now() - interval '1 minute' WHERE customer_id = CAST(:c AS uuid)",
         c=cid)
    k2 = _key(client, h, expires_in_days=1)
    assert client.get(f"{DEV}/account", headers=_kh(k2)).status_code == 200
    _sql("UPDATE customer_api_keys SET expires_at = now() - interval '1 minute' WHERE id = CAST(:k AS uuid)",
         k=k2["key_id"])
    assert client.get(f"{DEV}/account", headers=_kh(k2)).status_code == 401      # expired

    other_h, _ = _user(client, "nodev")
    assert client.get(f"{DEV}/deployments", headers=other_h).status_code == 404  # no account yet


# ── pricing ─────────────────────────────────────────────────────────────────

def test_quote_uses_profitability_engine_with_margin_floor(client):
    h, _ = _dev(client)
    small = _quote(client, h, "checksum", CHK, runs=10)
    big = _quote(client, h, "checksum", {**CHK, "size_bytes": 4 * 1024 * 1024})
    urgent = _quote(client, h, "checksum", CHK, priority=900)
    enterprise = _quote(client, h, "checksum", CHK, resources={"tier": "ENTERPRISE"})
    for q in (small, big, urgent, enterprise):
        est, mx = Decimal(q["estimated_cost_usd"]), Decimal(q["maximum_cost_usd"])
        b = q["breakdown"]
        assert est >= Decimal(str(settings.PRICING_MIN_PRICE_USD)) and mx > est
        assert b["contribution_margin_pct"] >= settings.PRICING_MIN_CONTRIBUTION_MARGIN - 1e-9
        assert b["expected_net_usd"] > 0                        # profitable at the quoted price
        assert {"quote_id", "estimated_runtime_seconds", "expires_at", "fcu"} <= set(q)
    assert Decimal(small["estimated_total_usd"]) == Decimal(small["estimated_cost_usd"]) * 10
    assert big["estimated_runtime_seconds"] > small["estimated_runtime_seconds"]
    assert big["fcu"] > small["fcu"]
    assert Decimal(urgent["estimated_cost_usd"]) >= Decimal(small["estimated_cost_usd"])
    assert enterprise["breakdown"]["tier_multiplier"] == 1.2
    mon = _quote(client, h, "monitor", {"targets": ["https://example.com", "https://example.org"]})
    assert mon["breakdown"]["verification_usd"] > 0


def test_url_policy_protects_provider_networks(client):
    h, _ = _dev(client)
    bad = [("http_check", {"url": "http://example.com"}), ("http_check", {"url": "https://localhost/x"}),
           ("http_check", {"url": "https://192.168.1.1/admin"}), ("http_check", {"url": "https://10.0.0.8"}),
           ("http_check", {"url": "https://169.254.169.254/latest/meta-data"}),
           ("http_check", {"url": "https://[::1]/"}), ("http_check", {"url": "https://router.local/"}),
           ("http_check", {"url": "https://user:pw@example.com/"}), ("http_check", {"url": "https://example.com:22/"}),
           ("http_check", {"url": "https://example.com", "method": "POST"}),
           ("monitor", {"targets": [f"https://example.com/{i}" for i in range(11)]}),
           ("monitor", {"targets": []}), ("checksum", {"seed": "x", "size_bytes": 10 ** 9}),
           ("api_request", {"url": "https://example.com"}), ("benchmark", {})]
    for wt, payload in bad:
        r = client.post(f"{DEV}/quotes", headers=h, json={"workload_type": wt, "payload": payload})
        assert r.status_code == 400, (wt, payload, r.text)
    ok = _quote(client, h, "http_check", {"url": "https://example.com/health"})
    assert ok["normalized_payload"] == {"url": "https://example.com/health", "method": "GET", "timeout": 10}
    r = client.post(f"{DEV}/quotes", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                      "resources": {"gpu": 1}})
    assert r.status_code == 400


# ── Stripe top-ups + webhooks ───────────────────────────────────────────────

def test_topup_checkout_webhook_credits_exactly_once(client, stripe):
    h, cid = _dev(client)
    assert client.get(f"{DEV}/billing/status").json()["mode"] == "test"
    t = _topup(client, h, stripe, 25.0, cid)
    req = stripe.requests[-1]
    form = parse_qs(req.content.decode())
    assert req.headers["Authorization"] == f"Bearer {FAKE_SK}"
    assert req.headers["Idempotency-Key"] == f"farmos-topup-{t['topup_id']}"
    assert form["mode"] == ["payment"] and form["line_items[0][price_data][unit_amount]"] == ["2500"]
    assert form["metadata[farmos_topup_id]"] == [t["topup_id"]]
    assert form["payment_intent_data[metadata][farmos_customer_id]"] == [cid]
    assert _bal(client, h)["balance_usd"] == 0                     # redirect/creation never credits

    ev = _session_event(t)
    r1 = _post_event(client, ev)
    assert r1.status_code == 200 and r1.json()["status"] == "PROCESSED"
    r2 = _post_event(client, ev)                                   # Stripe retry, same event id
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    pi_ev = {"id": f"evt_{uuid4().hex[:24]}", "type": "payment_intent.succeeded", "livemode": False,
             "data": {"object": {"id": ev["data"]["object"]["payment_intent"], "object": "payment_intent",
                                 "amount": 2500, "amount_received": 2500, "currency": "usd",
                                 "metadata": {"farmos_topup_id": t["topup_id"], "farmos_customer_id": cid}}}}
    r3 = _post_event(client, pi_ev)                                # second event for the same payment
    assert r3.json()["status"] == "DUPLICATE"
    assert _bal(client, h)["balance_usd"] == Decimal("25")
    rows = _sql("SELECT count(*) FROM ledger_transactions WHERE idempotency_key = :k", k=f"funding:{t['topup_id']}")
    assert rows[0][0] == 1
    ev_rows = _sql("SELECT processing_status, payload_hash, related_customer_id::text, processed_at IS NOT NULL "
                   "FROM stripe_webhook_events WHERE stripe_event_id = :e", e=ev["id"])
    assert ev_rows[0][0] == "PROCESSED" and len(ev_rows[0][1]) == 64 and ev_rows[0][2] == cid and ev_rows[0][3]
    led = client.get(f"{DEV}/ledger", headers=h).json()
    assert led[0]["type"] == "CUSTOMER_FUNDING" and Decimal(led[0]["amount_usd"]) == Decimal("25")
    tops = client.get(f"{DEV}/topups", headers=h).json()
    assert tops[0]["status"] == "SUCCEEDED" and tops[0]["checkout_url"] is None


def test_webhook_rejects_bad_signatures_and_replays(client, stripe):
    h, cid = _dev(client)
    t = _topup(client, h, stripe, 10.0, cid)
    ev = _session_event(t)
    assert _post_event(client, ev, secret="whsec_wrong").status_code == 400
    assert _post_event(client, ev, header="").status_code == 400
    assert _post_event(client, ev, header="t=abc,v1=00").status_code == 400
    assert _post_event(client, ev, ts=int(time.time()) - 3600).status_code == 400      # replayed/old
    raw = json.dumps(ev).encode()
    sig = stripe_client.sign_payload(raw, WHSEC)
    tampered = raw.replace(b'"amount_total": 1000', b'"amount_total": 99999')
    assert client.post(f"{BASE}/webhooks/stripe", content=tampered, headers={"Stripe-Signature": sig}).status_code == 400
    assert _bal(client, h)["balance_usd"] == 0
    assert _sql("SELECT count(*) FROM stripe_webhook_events WHERE stripe_event_id = :e", e=ev["id"])[0][0] == 0


def test_webhook_mismatches_unpaid_and_live_events_never_credit(client, stripe):
    h, cid = _dev(client)
    t = _topup(client, h, stripe, 10.0, cid)
    r = _post_event(client, _session_event(t, amount=100000))
    assert r.json()["status"] == "REJECTED" and "amount mismatch" in r.json()["detail"]
    assert _post_event(client, _session_event(t, currency="eur")).json()["status"] == "REJECTED"
    assert _post_event(client, _session_event(t, paid=False)).json()["status"] == "IGNORED"
    assert _post_event(client, _session_event(t, livemode=True)).json()["status"] == "REJECTED"
    unknown = {"id": f"evt_{uuid4().hex}", "type": "customer.created", "livemode": False, "data": {"object": {}}}
    assert _post_event(client, unknown).json()["status"] == "IGNORED"
    assert _bal(client, h)["balance_usd"] == 0
    assert _sql("SELECT count(*) FROM risk_events WHERE subject_id = CAST(:t AS uuid) AND category = 'STRIPE_MISMATCH'",
                t=t["topup_id"])[0][0] >= 1
    exp = _session_event(t, etype="checkout.session.expired")
    assert _post_event(client, exp).json()["status"] == "PROCESSED"
    assert client.get(f"{DEV}/topups", headers=h).json()[0]["status"] == "EXPIRED"


def test_refund_and_dispute(client, stripe, admin):
    h, cid, t = _funded(client, stripe, 20.0)
    pi = _session_event(t)["data"]["object"]["payment_intent"]
    _sql("UPDATE customer_topups SET stripe_payment_intent_id = :p WHERE id = CAST(:t AS uuid)", p=pi, t=t["topup_id"])
    refund = lambda cents, eid=None: {"id": eid or f"evt_{uuid4().hex}", "type": "charge.refunded", "livemode": False,  # noqa: E731
                                      "data": {"object": {"id": "ch_test_1", "object": "charge", "payment_intent": pi,
                                                          "amount_refunded": cents, "currency": "usd"}}}
    assert _post_event(client, refund(500)).json()["status"] == "PROCESSED"
    assert _bal(client, h)["balance_usd"] == Decimal("15")
    assert _post_event(client, refund(500)).json()["detail"] == "nothing new to refund"   # cumulative, no double
    assert _post_event(client, refund(800)).json()["status"] == "PROCESSED"
    assert _bal(client, h)["balance_usd"] == Decimal("12")
    dispute = {"id": f"evt_{uuid4().hex}", "type": "charge.dispute.created", "livemode": False,
               "data": {"object": {"id": "dp_test_1", "object": "dispute", "payment_intent": pi, "amount": 2000}}}
    assert _post_event(client, dispute).json()["status"] == "PROCESSED"
    acct = client.get(f"{DEV}/account", headers=h).json()
    assert acct["status"] == "ON_HOLD"
    r = client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                           "max_price_usd": 1})
    assert r.status_code == 403
    assert client.post(f"{BASE}/admin/developer/customers/{cid}/release", headers=admin).json()["status"] == "ACTIVE"


def test_live_keys_refused_unless_explicitly_enabled(client, stripe, monkeypatch):
    h, _ = _dev(client)
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_live_" + "0" * 24)
    st = client.get(f"{DEV}/billing/status").json()
    assert st["mode"] == "live_disabled" and st["checkout_enabled"] is False and st["book"] == "TEST"
    n = len(stripe.requests)
    r = client.post(f"{DEV}/topups", headers=h, json={"amount_usd": 10})
    assert r.status_code == 503 and len(stripe.requests) == n         # nothing sent to Stripe
    assert client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                              "max_price_usd": 1}).status_code == 503
    monkeypatch.setattr(settings, "STRIPE_LIVE_MODE_ENABLED", True)   # still not production
    assert client.get(f"{DEV}/billing/status").json()["mode"] == "live_disabled"
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    assert client.post(f"{DEV}/topups", headers=h, json={"amount_usd": 10}).status_code == 503


def test_topup_validation_idempotency_and_stripe_errors(client, stripe):
    h, _ = _dev(client)
    assert client.post(f"{DEV}/topups", headers=h, json={"amount_usd": 1}).status_code == 400
    assert client.post(f"{DEV}/topups", headers=h, json={"amount_usd": 5000}).status_code == 400
    key = uuid4().hex
    a = client.post(f"{DEV}/topups", headers={**h, "Idempotency-Key": key}, json={"amount_usd": 10})
    b = client.post(f"{DEV}/topups", headers={**h, "Idempotency-Key": key}, json={"amount_usd": 10})
    assert a.json()["topup_id"] == b.json()["topup_id"] and b.json()["idempotent_replay"] is True
    assert client.post(f"{DEV}/topups", headers={**h, "Idempotency-Key": key},
                       json={"amount_usd": 11}).status_code == 422
    pi = client.post(f"{DEV}/topups", headers=h, json={"amount_usd": 10, "method": "payment_intent"}).json()
    assert pi["client_secret"].startswith("pi_test_")
    stripe.fail = True
    r = client.post(f"{DEV}/topups", headers=h, json={"amount_usd": 10})
    assert r.status_code == 502
    assert client.get(f"{DEV}/topups", headers=h).json()[0]["status"] == "FAILED"


# ── deployments + billing lifecycle ─────────────────────────────────────────

def test_insufficient_funds_returns_402(client, stripe):
    h, _ = _dev(client)
    r = client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                           "max_price_usd": 1})
    assert r.status_code == 402 and r.json()["detail"]["error"] == "insufficient_funds"
    assert client.get(f"{DEV}/deployments", headers=h).json() == []


def test_deployment_reserve_charge_on_verification_and_revenue_confirmed(client, admin, stripe):
    h, cid, _ = _funded(client, stripe, 20.0)
    q = _quote(client, h)
    dep = _deploy(client, h, quote_id=q["quote_id"], name="nightly checksum")
    assert dep["status"] == "COMPLETED" and dep["runs_created"] == 1 and dep["test_mode"] is True
    assert dep["price_per_run_usd"] == q["estimated_cost_usd"]
    price, mx = Decimal(q["estimated_cost_usd"]), Decimal(q["maximum_cost_usd"])
    b = _bal(client, h)
    assert b["balance_usd"] == 20 and b["reserved_usd"] == mx and b["available_usd"] == 20 - mx

    run = _runs(client, h, dep["deployment_id"])[0]
    assert run["job_status"] == "QUEUED" and run["charge_status"] == "RESERVED" and run["result"] is None
    node = _ready_node(client, admin)
    _isolate(node)
    t = _tick(client, admin, run["job_id"])
    assert run["job_id"] in [d["job_id"] for d in t["dispatched"]], t
    out = _complete(node, run["job_id"], CHK)["verification"]
    assert out["verification_status"] == "PASSED"
    assert out["customer_billing"]["revenue"] == "confirmed"

    b = _bal(client, h)
    assert b["balance_usd"] == 20 - price and b["reserved_usd"] == 0       # charged the price, not the max
    run = _runs(client, h, dep["deployment_id"])[0]
    assert run["charge_status"] == "CHARGED" and Decimal(run["charged_usd"]) == price
    assert run["result"]["output_sha256"] and run["result_hash"]
    econ = _sql("SELECT revenue_state, confirmation_source, ledger_book, confirmed_gross_usd FROM job_economics "
                "WHERE job_id = CAST(:j AS uuid)", j=run["job_id"])[0]
    assert econ[0] == "CONFIRMED" and econ[1] == "customer_prepaid" and econ[2] == "TEST"
    assert Decimal(str(econ[3])) == price
    # prepaid jobs skip the simulated settlement: no settle:<job> transaction
    assert _sql("SELECT count(*) FROM ledger_transactions WHERE idempotency_key = :k",
                k=f"settle:{run['job_id']}")[0][0] == 0
    assert _sql("SELECT count(*) FROM ledger_transactions WHERE idempotency_key = :k",
                k=f"custcharge:{run['job_id']}")[0][0] == 1
    single = client.get(f"{DEV}/jobs/{run['job_id']}", headers=h)
    assert single.status_code == 200 and single.json()["deployment_id"] == dep["deployment_id"]
    other_h, _ = _dev(client)
    assert client.get(f"{DEV}/jobs/{run['job_id']}", headers=other_h).status_code == 404
    assert client.get(f"{DEV}/deployments/{dep['deployment_id']}", headers=other_h).status_code == 404

    rec = client.get(f"{BASE}/admin/developer/reconcile?book=TEST", headers=admin).json()
    assert rec["ok"] is True, rec
    assert rec["unbalanced_transactions"] == []


def test_failed_run_releases_reservation_and_is_never_charged(client, admin, stripe):
    h, _, _ = _funded(client, stripe, 10.0)
    dep = _deploy(client, h)
    run = _runs(client, h, dep["deployment_id"])[0]
    assert _bal(client, h)["reserved_usd"] > 0
    _sql("UPDATE jobs SET deadline_at = now() - interval '1 minute' WHERE id = CAST(:j AS uuid)", j=run["job_id"])
    t = _tick(client, admin, run["job_id"])
    assert run["job_id"] in t["expired"]
    rep = _admin_tick(client, admin)
    assert rep["settlement"]["released"] >= 1
    b = _bal(client, h)
    assert b["balance_usd"] == 10 and b["reserved_usd"] == 0 and b["available_usd"] == 10
    run = _runs(client, h, dep["deployment_id"])[0]
    assert run["charge_status"] == "RELEASED" and run["charged_usd"] == "0.000000"


def test_recurring_schedule_pause_on_funds_resume_and_cancel(client, admin, stripe):
    h, cid, _ = _funded(client, stripe, 5.0)
    dep = _deploy(client, h, schedule={"interval_seconds": 60, "max_runs": 3})
    did = dep["deployment_id"]
    assert dep["status"] == "ACTIVE" and dep["runs_created"] == 1 and dep["next_run_at"]
    assert _admin_tick(client, admin)["runs_created"] == 0              # not due yet

    def due():
        _sql("UPDATE deployments SET next_run_at = now() - interval '1 second' WHERE id = CAST(:d AS uuid)", d=did)
    due()
    _admin_tick(client, admin)
    assert client.get(f"{DEV}/deployments/{did}", headers=h).json()["runs_created"] == 2

    # drain the balance -> next run pauses the deployment instead of overspending
    _sql("UPDATE customer_job_charges SET reserved_usd = 4.9999 WHERE deployment_id = CAST(:d AS uuid) "
         "AND run_index = 2", d=did)
    due()
    _admin_tick(client, admin)
    d = client.get(f"{DEV}/deployments/{did}", headers=h).json()
    assert d["status"] == "PAUSED_FUNDS" and d["runs_created"] == 2
    _sql("UPDATE customer_job_charges SET reserved_usd = price_usd * 1.2 WHERE deployment_id = CAST(:d AS uuid)", d=did)
    rep = _admin_tick(client, admin)                                     # funds available again
    assert rep["resumed"] >= 1
    _admin_tick(client, admin)
    d = client.get(f"{DEV}/deployments/{did}", headers=h).json()
    assert d["runs_created"] == 3 and d["status"] == "COMPLETED" and d["next_run_at"] is None
    due()
    _admin_tick(client, admin)
    assert client.get(f"{DEV}/deployments/{did}", headers=h).json()["runs_created"] == 3   # bounded

    dep2 = _deploy(client, h, schedule={"interval_seconds": 120, "max_runs": 10})
    c = client.post(f"{DEV}/deployments/{dep2['deployment_id']}/cancel", headers=h).json()
    assert c["status"] == "CANCELLED" and c["cancelled_jobs"] == 1
    assert _runs(client, h, dep2["deployment_id"])[0]["charge_status"] == "RELEASED"
    assert all(r["charge_status"] != "RESERVED" or r["job_status"] == "QUEUED"
               for r in _runs(client, h, did))
    bad = client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                             "max_price_usd": 1,
                                                             "schedule": {"interval_seconds": 10}})
    assert bad.status_code == 400
    unbounded = client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                                   "max_price_usd": 1,
                                                                   "schedule": {"interval_seconds": 60}})
    assert unbounded.status_code == 400 and "bounded" in unbounded.text


def test_deployment_idempotency_quote_binding_and_max_price(client, stripe):
    h, _, _ = _funded(client, stripe, 10.0)
    q = _quote(client, h)
    key = uuid4().hex
    a = _deploy(client, h, quote_id=q["quote_id"], key=key)
    b = client.post(f"{DEV}/deployments", headers={**h, "Idempotency-Key": key},
                    json={"workload_type": "checksum", "payload": CHK, "quote_id": q["quote_id"]})
    assert b.status_code == 201 and b.json()["deployment_id"] == a["deployment_id"]
    assert b.json()["idempotent_replay"] is True
    assert len(client.get(f"{DEV}/deployments", headers=h).json()) == 1
    assert client.post(f"{DEV}/deployments", headers={**h, "Idempotency-Key": key},
                       json={"workload_type": "checksum", "payload": {**CHK, "seed": "other"},
                             "quote_id": q["quote_id"]}).status_code == 422
    # a quote is bound to its spec
    r = client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum",
                                                           "payload": {**CHK, "size_bytes": 1024},
                                                           "quote_id": q["quote_id"]})
    assert r.status_code == 409
    _sql("UPDATE price_quotes SET expires_at = now() - interval '1 second' WHERE id = CAST(:q AS uuid)", q=q["quote_id"])
    assert client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                              "quote_id": q["quote_id"]}).status_code == 409
    assert client.post(f"{DEV}/deployments", headers=h, json={"workload_type": "checksum", "payload": CHK,
                                                              "max_price_usd": 0.0000001}).status_code == 409
    assert client.post(f"{DEV}/deployments", headers=h,
                       json={"workload_type": "checksum", "payload": CHK}).status_code == 400


def test_customer_webhook_destination_signed_retried(client, admin, stripe, hooks):
    received, status = hooks
    h, _, _ = _funded(client, stripe, 10.0)
    key_no_hooks = _key(client, h, scopes=["jobs:read", "jobs:write", "billing:read"])
    r = client.post(f"{DEV}/deployments", headers=_kh(key_no_hooks),
                    json={"workload_type": "checksum", "payload": CHK, "max_price_usd": 1,
                          "destination": {"type": "webhook", "url": "https://hooks.example.com/farmos"}})
    assert r.status_code == 403                                              # webhooks:write needed
    bad = client.post(f"{DEV}/deployments", headers=h,
                      json={"workload_type": "checksum", "payload": CHK, "max_price_usd": 1,
                            "destination": {"type": "webhook", "url": "https://127.0.0.1/hook"}})
    assert bad.status_code == 400
    status["code"] = 500
    dep = _deploy(client, h, destination={"type": "webhook", "url": "https://hooks.example.com/farmos"})
    secret = dep["webhook_secret"]
    assert secret.startswith("whsec_farmos_")
    assert "webhook_secret" not in client.get(f"{DEV}/deployments/{dep['deployment_id']}", headers=h).json()
    _admin_tick(client, admin)
    assert received, "job.created should have been attempted"
    dl = client.get(f"{DEV}/deployments/{dep['deployment_id']}/webhook-deliveries", headers=h).json()
    assert dl[0]["status"] == "PENDING" and dl[0]["attempts"] == 1 and dl[0]["last_status_code"] == 500
    status["code"] = 200
    _sql("UPDATE customer_webhook_deliveries SET next_attempt_at = now() WHERE deployment_id = CAST(:d AS uuid)",
         d=dep["deployment_id"])
    _admin_tick(client, admin)
    dl = client.get(f"{DEV}/deployments/{dep['deployment_id']}/webhook-deliveries", headers=h).json()
    assert dl[0]["status"] == "DELIVERED" and dl[0]["attempts"] == 2
    req = received[-1]
    assert req.headers["FARMOS-Event-Type"] == "job.created" and req.headers["FARMOS-Event-Id"] == dl[0]["delivery_id"]
    assert verify_webhook_signature(secret, req.content, req.headers["FARMOS-Signature"])
    assert not verify_webhook_signature("wrong", req.content, req.headers["FARMOS-Signature"])
    body = json.loads(req.content)
    assert body["type"] == "job.created" and body["test_mode"] is True


# ── SDK + CLI ───────────────────────────────────────────────────────────────

def _cli_out(capsys):
    """The CLI pretty-prints one JSON document; app log lines (single-line JSON) may interleave."""
    text = capsys.readouterr().out
    start = text.find("{\n") if text.find("{\n") != -1 else text.find("[\n")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def test_python_sdk_and_cli_against_api(client, stripe, capsys):
    h, cid, _ = _funded(client, stripe, 10.0)
    token = h["Authorization"].split()[1]
    dash = FarmosClient(token=token, http=client)
    key = dash.create_key("sdk", ["jobs:read", "jobs:write", "billing:read", "billing:write"])
    fos = FarmosClient(api_key=key["api_key"], http=client)
    assert fos.account()["customer_id"] == cid
    assert Decimal(fos.balance()["balance_usd"]) == 10
    q = fos.quote("checksum", CHK, runs=5)
    dep = fos.create_deployment("checksum", CHK, quote_id=q["quote_id"], name="sdk")
    assert fos.deployment(dep["deployment_id"])["name"] == "sdk"
    assert fos.runs(dep["deployment_id"])[0]["charge_status"] == "RESERVED"
    assert fos.topup(10)["status"] == "PENDING"
    with pytest.raises(FarmosError) as exc:
        fos.create_deployment("http_check", {"url": "https://10.1.2.3"}, max_price_usd=1)
    assert exc.value.status_code == 400
    assert cli_run(["balance"], client=fos) == 0
    assert _cli_out(capsys)["test_mode"] is True
    assert cli_run(["deploy", "checksum", json.dumps(CHK), "--max-price", "1", "--every", "60", "--runs", "2"],
                   client=fos) == 0
    out = _cli_out(capsys)
    assert out["schedule"] == {"interval_seconds": 60, "max_runs": 2}
    assert cli_run(["cancel", out["deployment_id"]], client=fos) == 0
    assert _cli_out(capsys)["status"] == "CANCELLED"
    assert cli_run(["quote", "http_check", '{"url":"http://insecure.example"}'], client=fos) == 1
    assert dash.revoke_key(key["key_id"])["active"] is False
    with pytest.raises(FarmosError) as exc:
        fos.balance()
    assert exc.value.status_code == 401


def test_admin_views(client, admin, stripe):
    _funded(client, stripe, 7.0)
    assert client.get(f"{BASE}/admin/developer/customers", headers=admin).status_code == 200
    ev = client.get(f"{BASE}/admin/developer/stripe-events", headers=admin).json()
    assert ev and {"stripe_event_id", "payload_hash", "processing_status"} <= set(ev[0])
    h, _ = _dev(client)
    assert client.get(f"{BASE}/admin/developer/reconcile", headers=h).status_code == 403
