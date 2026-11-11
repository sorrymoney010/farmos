"""Sprint 8/9 integration: double-entry ledger, simulated revenue confirmation +
split, SIWE wallet verification, controlled withdrawals, mock-signed simulated
payouts, reconciliation, daily close, kill switches.

TEST book + simulated testnet chain only. Wallet keys are ephemeral,
generated in-process for the test and never persisted. No real funds.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.payouts import simulated
from app.services.finance import revenue
from app.tests.integration.test_sprint3_benchmarks_safety import BASE, admin, client  # noqa: F401
from app.tests.integration.test_sprint4_5_leases_verification import (  # noqa: F401
    CHK, _create, _isolate, _no_random_redundancy, _ready_node, _sql,
)
from app.tests.integration.test_sprint6_7_opportunities_scheduler import _complete, _tick

FIN = f"{BASE}/admin/finance"
PW = "ChangeMe123!"


# ── helpers ──────────────────────────────────────────────────────────────────

def _login(client, email):
    r = client.post(f"{BASE}/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _user(client, prefix="user"):
    email = f"{prefix}-{uuid4().hex[:10]}@farmos.example"
    assert client.post(f"{BASE}/auth/register", json={"email": email, "password": PW}).status_code == 201
    h = _login(client, email)
    return h, client.get(f"{BASE}/auth/me", headers=h).json()["user_id"]


def _provider(client, age_days=None):
    h, uid = _user(client, "prov")
    r = client.post(f"{BASE}/provider/onboard", headers=h, json={"display_name": "Test Farm"})
    assert r.status_code == 200, r.text
    pid = r.json()["provider_id"]
    if age_days:
        _sql(f"UPDATE providers SET created_at = now() - interval '{int(age_days)} days' WHERE id = CAST(:p AS uuid)",
             p=pid)
    return h, pid


def _second_admin(client):
    h, uid = _user(client, "admin2")
    _sql("INSERT INTO user_roles (id, user_id, role_id, created_at) "
         "SELECT gen_random_uuid(), CAST(:u AS uuid), id, now() FROM roles WHERE name = 'ADMIN'", u=uid)
    return h


def _sign(acct, message):
    return acct.sign_message(encode_defunct(text=message)).signature.hex()


def _wallet(client, h, acct=None, network="base-sepolia"):
    acct = acct or Account.create()
    ch = client.post(f"{BASE}/wallets/challenge", headers=h, json={"address": acct.address, "network": network})
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    v = client.post(f"{BASE}/wallets/verify", headers=h,
                    json={"challenge_id": ch["challenge_id"], "signature": _sign(acct, ch["message"])})
    assert v.status_code == 200, v.text
    return v.json(), acct


def _grant(client, admin, pid, amount):
    r = client.post(f"{FIN}/test-grant", headers={**admin, "Idempotency-Key": uuid4().hex},
                    json={"provider_id": pid, "amount": str(amount)})
    assert r.status_code == 200, r.text
    return r.json()


def _withdraw(client, h, wallet_id, amount, key=None, book="TEST"):
    return client.post(f"{BASE}/withdrawals/", headers={**h, "Idempotency-Key": key or uuid4().hex},
                       json={"wallet_id": wallet_id, "amount": str(amount), "book": book})


def _eligible(wid):
    _sql("UPDATE withdrawals SET eligible_at = now() - interval '1 minute' WHERE id = CAST(:w AS uuid)", w=wid)


def _bal(client, h, book="TEST"):
    return {k: Decimal(v) if k not in ("asset", "simulated") else v
            for k, v in client.get(f"{BASE}/provider/balances", headers=h).json()["balances"][book].items()}


def _approve(client, h, wid):
    return client.post(f"{FIN}/withdrawals/{wid}/approve", headers=h, json={})


def _settle(client, admin):
    rb = client.post(f"{FIN}/payouts/run-batch", headers=admin)
    assert rb.status_code == 200, rb.text
    polls = [client.post(f"{FIN}/payouts/poll", headers=admin).json() for _ in range(4)]
    return rb.json(), polls


def _approved_withdrawal(client, admin, amount=Decimal("20")):
    h, pid = _provider(client, age_days=30)
    _grant(client, admin, pid, amount * 2)
    w, acct = _wallet(client, h)
    r = _withdraw(client, h, w["wallet_id"], amount)
    assert r.status_code == 200, r.text
    wd = r.json()
    _eligible(wd["withdrawal_id"])
    a = _approve(client, admin, wd["withdrawal_id"])
    assert a.status_code == 200, a.text
    assert a.json()["status"] == "APPROVED"
    return h, pid, w, wd


@pytest.fixture(autouse=True)
def _switches_off(client, admin):
    def off():
        for n in ("PAUSE_WITHDRAWALS", "PAUSE_PAYOUTS", "PAUSE_REVENUE_CONFIRMATION"):
            client.post(f"{FIN}/kill-switches", headers=admin, json={"name": n, "enabled": False, "reason": "test"})
    off()
    yield
    off()
    simulated.FAIL_PAYOUT_IDS.clear()
    revenue.DISPUTED_JOB_IDS.clear()


# ── ledger ───────────────────────────────────────────────────────────────────

def test_test_grant_is_balanced_idempotent_and_immutable(client, admin):
    h, pid = _provider(client)
    key = uuid4().hex
    hdr = {**admin, "Idempotency-Key": key}
    r1 = client.post(f"{FIN}/test-grant", headers=hdr, json={"provider_id": pid, "amount": "12.5"})
    r2 = client.post(f"{FIN}/test-grant", headers=hdr, json={"provider_id": pid, "amount": "12.5"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["created"] is True and r2.json()["created"] is False
    assert r1.json()["transaction_id"] == r2.json()["transaction_id"]
    assert _bal(client, h)["available"] == Decimal("12.5")
    assert _bal(client, h, "REAL")["available"] == 0  # TEST credits never touch REAL USDC

    tx = client.get(f"{FIN}/transactions/{r1.json()['transaction_id']}", headers=admin).json()
    assert tx["tx_type"] == "TEST_CREDIT" and tx["book"] == "TEST"
    deb = sum(Decimal(e["amount"]) for e in tx["entries"] if e["direction"] == "DEBIT")
    cre = sum(Decimal(e["amount"]) for e in tx["entries"] if e["direction"] == "CREDIT")
    assert deb == cre == Decimal("12.5") and {e["asset"] for e in tx["entries"]} == {"TEST_USD"}
    tb = client.get(f"{FIN}/trial-balance?book=TEST", headers=admin).json()
    assert tb["balanced"] is True and tb["unbalanced_transactions"] == []

    # append-only at the database level
    with pytest.raises(Exception, match="append-only"):
        _sql("UPDATE ledger_entries SET amount = 999 WHERE transaction_id = CAST(:t AS uuid)",
             t=r1.json()["transaction_id"])
    with pytest.raises(Exception, match="append-only"):
        _sql("DELETE FROM ledger_transactions WHERE id = CAST(:t AS uuid)", t=r1.json()["transaction_id"])

    # provider ledger view only shows the provider's own movements
    led = client.get(f"{BASE}/provider/ledger", headers=h).json()
    assert len(led) == 1 and led[0]["code"] == "2000" and led[0]["direction"] == "CREDIT"

    # treasury cash postings mirror (simulated) chain events: no blind reversal
    rv = client.post(f"{FIN}/transactions/{r1.json()['transaction_id']}/reverse", headers=admin,
                     json={"reason": "test"})
    assert rv.status_code == 409 and "compensating" in rv.json()["detail"]


def test_finance_admin_requires_finance_role(client, admin):
    h, _ = _provider(client)
    assert client.get(f"{FIN}/trial-balance", headers=h).status_code == 403
    assert client.post(f"{FIN}/test-grant", headers={**h, "Idempotency-Key": "x"},
                       json={"provider_id": str(uuid4()), "amount": "1"}).status_code == 403
    assert client.get(f"{FIN}/chart", headers=admin).status_code == 200


# ── revenue confirmation + split ─────────────────────────────────────────────

def _verified_job(client, admin):
    node = _ready_node(client, admin)
    _sql("UPDATE devices SET reputation_score = 99 WHERE id = CAST(:i AS uuid)", i=node.id)
    _isolate(node)
    job = _create(client, admin, "checksum", CHK)["job_id"]
    assert _tick(client, admin, job)["dispatched"][0]["device_id"] == node.id
    assert _complete(node, job, CHK)["status"] == "VERIFIED"
    return job


def test_revenue_confirmation_split_and_release(client, admin):
    job = _verified_job(client, admin)
    before = _bal(client, admin)
    rep = client.post(f"{FIN}/revenue/confirm", headers=admin, json={"job_ids": [job]})
    assert rep.status_code == 200, rep.text
    rep = rep.json()
    assert rep["book"] == "TEST" and rep["source"] == "simulated_billing"
    c = rep["confirmed"][0]
    assert c["job_id"] == job and Decimal(c["gross"]) == Decimal("0.002")
    assert Decimal(c["provider_share"]) == Decimal("0.0013") and Decimal(c["infrastructure"]) == Decimal("0.0003")
    assert Decimal(c["farmos"]) == Decimal("0.0004")
    after = _bal(client, admin)
    assert after["pending"] - before["pending"] == Decimal("0.0013")
    assert after["available"] == before["available"]
    econ = client.get(f"{BASE}/admin/economics/jobs/{job}", headers=admin).json()
    assert econ["revenue_state"] == "CONFIRMED"
    # idempotent: confirming again does nothing
    assert client.post(f"{FIN}/revenue/confirm", headers=admin, json={"job_ids": [job]}).json()["confirmed"] == []

    # confirmation hold: nothing becomes available yet
    rel = client.post(f"{FIN}/revenue/release-pending", headers=admin).json()
    assert job not in [r["job_id"] for r in rel["released"]]
    _sql("UPDATE job_economics SET confirmed_at = now() - interval '25 hours' WHERE job_id = CAST(:j AS uuid)", j=job)
    rel = client.post(f"{FIN}/revenue/release-pending", headers=admin).json()
    assert job in [r["job_id"] for r in rel["released"]]
    final = _bal(client, admin)
    assert final["available"] - before["available"] == Decimal("0.0013")
    assert final["pending"] == before["pending"]

    txs = client.get(f"{FIN}/transactions?tx_type=JOB_REVENUE_CONFIRMED", headers=admin).json()
    t = next(t for t in txs if t["reference_id"] == job)
    full = client.get(f"{FIN}/transactions/{t['transaction_id']}", headers=admin).json()
    codes = {(e["code"], e["direction"]) for e in full["entries"]}
    assert codes == {("1020", "DEBIT"), ("2010", "CREDIT"), ("4045", "CREDIT"), ("4000", "CREDIT")}
    assert client.get(f"{FIN}/trial-balance", headers=admin).json()["balanced"] is True


def test_revenue_reversal_restores_pending(client, admin):
    job = _verified_job(client, admin)
    before = _bal(client, admin)
    client.post(f"{FIN}/revenue/confirm", headers=admin, json={"job_ids": [job]})
    assert _bal(client, admin)["pending"] - before["pending"] == Decimal("0.0013")
    t = next(t for t in client.get(f"{FIN}/transactions?tx_type=JOB_REVENUE_CONFIRMED", headers=admin).json()
             if t["reference_id"] == job)
    rev = client.post(f"{FIN}/transactions/{t['transaction_id']}/reverse", headers=admin, json={"reason": "chargeback"})
    assert rev.status_code == 200, rev.text
    assert rev.json()["tx_type"] == "REVERSAL" and rev.json()["reverses_transaction_id"] == t["transaction_id"]
    after = _bal(client, admin)
    assert after["pending"] == before["pending"] and after["lifetime_earned"] == before["lifetime_earned"]
    assert client.get(f"{BASE}/admin/economics/jobs/{job}", headers=admin).json()["revenue_state"] == "REVERSED"
    # idempotent, and a reversal cannot itself be reversed
    again = client.post(f"{FIN}/transactions/{t['transaction_id']}/reverse", headers=admin, json={"reason": "x"})
    assert again.json()["transaction_id"] == rev.json()["transaction_id"]
    assert client.post(f"{FIN}/transactions/{rev.json()['transaction_id']}/reverse", headers=admin,
                       json={"reason": "nope"}).status_code == 409
    assert client.get(f"{FIN}/trial-balance", headers=admin).json()["balanced"] is True


def test_disputed_revenue_and_paused_confirmation(client, admin):
    job = _verified_job(client, admin)
    client.post(f"{FIN}/kill-switches", headers=admin,
                json={"name": "PAUSE_REVENUE_CONFIRMATION", "enabled": True, "reason": "test"})
    assert client.post(f"{FIN}/revenue/confirm", headers=admin, json={"job_ids": [job]}).status_code == 409
    client.post(f"{FIN}/kill-switches", headers=admin, json={"name": "PAUSE_REVENUE_CONFIRMATION", "enabled": False})
    revenue.DISPUTED_JOB_IDS.add(job)
    before = _bal(client, admin)
    rep = client.post(f"{FIN}/revenue/confirm", headers=admin, json={"job_ids": [job]}).json()
    assert rep["disputed"] == [job] and rep["confirmed"] == []
    assert _bal(client, admin)["pending"] == before["pending"]
    assert client.get(f"{BASE}/admin/economics/jobs/{job}", headers=admin).json()["revenue_state"] == "DISPUTED"


def test_split_policy_validation(client, admin):
    bad = client.put(f"{FIN}/revenue/split-policy", headers=admin,
                     json={"default": {"provider": 0.9, "infrastructure": 0.2, "farmos": 0.1}})
    assert bad.status_code == 400
    ok = client.put(f"{FIN}/revenue/split-policy", headers=admin,
                    json={"default": {"provider": 0.65, "infrastructure": 0.15, "farmos": 0.20},
                          "overrides": {"acurast_sim": {"provider": 0.5, "infrastructure": 0.2, "farmos": 0.3}}})
    assert ok.status_code == 200, ok.text
    got = client.get(f"{FIN}/revenue/split-policy", headers=admin).json()
    assert got["overrides"]["acurast_sim"]["provider"] == 0.5
    client.put(f"{FIN}/revenue/split-policy", headers=admin,
               json={"default": {"provider": 0.65, "infrastructure": 0.15, "farmos": 0.20}, "overrides": {}})


# ── wallets ──────────────────────────────────────────────────────────────────

def test_wallet_siwe_verification_replay_and_wrong_signer(client, admin):
    h, pid = _provider(client)
    acct = Account.create()
    ch = client.post(f"{BASE}/wallets/challenge", headers=h,
                     json={"address": acct.address.lower(), "network": "base-sepolia"}).json()
    assert ch["address"] == acct.address and ch["chain_id"] == 84532
    assert "does not authorize a transfer" in ch["message"] and "Chain ID: 84532" in ch["message"]

    # wrong signer -> 400 and attempt recorded; wallet stays unverified
    other = Account.create()
    bad = client.post(f"{BASE}/wallets/verify", headers=h,
                      json={"challenge_id": ch["challenge_id"], "signature": _sign(other, ch["message"])})
    assert bad.status_code == 400 and "not from this address" in bad.json()["detail"]
    ws = client.get(f"{BASE}/wallets/", headers=h).json()
    assert ws[0]["is_verified"] is False

    good = client.post(f"{BASE}/wallets/verify", headers=h,
                       json={"challenge_id": ch["challenge_id"], "signature": _sign(acct, ch["message"])})
    assert good.status_code == 200, good.text
    g = good.json()
    assert g["is_verified"] and g["is_default"] and g["verification_method"] == "EIP-191/SIWE"
    assert g["in_cooldown"] is False and g["payouts_supported"] is True
    # replay of a consumed challenge
    rp = client.post(f"{BASE}/wallets/verify", headers=h,
                     json={"challenge_id": ch["challenge_id"], "signature": _sign(acct, ch["message"])})
    assert rp.status_code == 409
    # another user cannot use my challenge
    h2, _ = _provider(client)
    assert client.post(f"{BASE}/wallets/verify", headers=h2,
                       json={"challenge_id": ch["challenge_id"], "signature": _sign(acct, ch["message"])}
                       ).status_code == 404
    # a second wallet on an existing account gets a security cooldown; switching default too
    w2, _ = _wallet(client, h)
    assert w2["in_cooldown"] is True and w2["is_default"] is False
    sd = client.post(f"{BASE}/wallets/{w2['wallet_id']}/default", headers=h)
    assert sd.status_code == 200 and sd.json()["is_default"] is True


def test_wallet_challenge_expiry_and_lockout(client, admin):
    h, _ = _provider(client)
    acct = Account.create()
    ch = client.post(f"{BASE}/wallets/challenge", headers=h, json={"address": acct.address}).json()
    _sql("UPDATE wallet_verification_challenges SET expires_at = now() - interval '1 minute' "
         "WHERE id = CAST(:c AS uuid)", c=ch["challenge_id"])
    r = client.post(f"{BASE}/wallets/verify", headers=h,
                    json={"challenge_id": ch["challenge_id"], "signature": _sign(acct, ch["message"])})
    assert r.status_code == 400 and "expired" in r.json()["detail"]

    ch = client.post(f"{BASE}/wallets/challenge", headers=h, json={"address": acct.address}).json()
    other = Account.create()
    for _ in range(5):
        client.post(f"{BASE}/wallets/verify", headers=h,
                    json={"challenge_id": ch["challenge_id"], "signature": _sign(other, ch["message"])})
    locked = client.post(f"{BASE}/wallets/verify", headers=h,
                         json={"challenge_id": ch["challenge_id"], "signature": _sign(acct, ch["message"])})
    assert locked.status_code == 409  # challenge burned after max failed attempts
    assert client.post(f"{BASE}/wallets/challenge", headers=h,
                       json={"address": acct.address, "network": "ethereum"}).status_code == 400


# ── withdrawals ──────────────────────────────────────────────────────────────

def test_withdrawal_guards(client, admin):
    h, pid = _provider(client)            # new account: 100/day limit
    _grant(client, admin, pid, 300)
    acct = Account.create()
    ch = client.post(f"{BASE}/wallets/challenge", headers=h, json={"address": acct.address}).json()
    unverified = ch["wallet_id"]
    assert _withdraw(client, h, unverified, 10).status_code == 409  # unverified wallet
    w, _ = _wallet(client, h, acct)
    wid = w["wallet_id"]
    no_key = client.post(f"{BASE}/withdrawals/", headers=h, json={"wallet_id": wid, "amount": "10"})
    assert no_key.status_code == 400  # Idempotency-Key required
    assert _withdraw(client, h, wid, 1).status_code == 400             # below minimum
    assert _withdraw(client, h, wid, "1.0000001").status_code == 422   # > 6 decimals
    assert _withdraw(client, h, wid, 150).status_code == 409           # new-account daily limit
    assert _withdraw(client, h, wid, 10, book="REAL").status_code == 409  # no REAL withdrawals in this build
    base_w, _ = _wallet(client, h, network="base")                     # mainnet wallet: ownership only
    assert _withdraw(client, h, base_w["wallet_id"], 10).status_code == 409

    key = uuid4().hex
    r1, r2 = _withdraw(client, h, wid, 60, key), _withdraw(client, h, wid, 60, key)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["withdrawal_id"] == r2.json()["withdrawal_id"] and r2.json()["idempotent_replay"] is True
    assert _withdraw(client, h, wid, 61, key).status_code == 409       # key reuse with other params
    wd = r1.json()
    assert wd["status"] == "SECURITY_HOLD" and wd["required_approvals"] == 1 and wd["hold_remaining_seconds"] > 0
    assert "account younger than 7 days" in wd["risk_reasons"]
    b = _bal(client, h)
    assert b["available"] == 240 and b["held"] == 60
    assert _withdraw(client, h, wid, 50).status_code == 409            # 60 + 50 > 100 daily

    # security hold: cannot approve early; provider cannot approve
    assert _approve(client, admin, wd["withdrawal_id"]).status_code == 409
    assert _approve(client, h, wd["withdrawal_id"]).status_code == 403
    # cancel releases the hold
    c = client.post(f"{BASE}/withdrawals/{wd['withdrawal_id']}/cancel", headers=h)
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    b = _bal(client, h)
    assert b["available"] == 300 and b["held"] == 0
    assert client.post(f"{BASE}/withdrawals/{wd['withdrawal_id']}/cancel", headers=h).status_code == 409

    # kill switch
    client.post(f"{FIN}/kill-switches", headers=admin, json={"name": "PAUSE_WITHDRAWALS", "enabled": True})
    assert _withdraw(client, h, wid, 10).status_code == 409
    client.post(f"{FIN}/kill-switches", headers=admin, json={"name": "PAUSE_WITHDRAWALS", "enabled": False})

    # insufficient balance
    h3, pid3 = _provider(client)
    w3, _ = _wallet(client, h3)
    assert _withdraw(client, h3, w3["wallet_id"], 10).status_code == 409


def test_wallet_cooldown_extends_hold_and_payout_hold_blocks(client, admin):
    h, pid = _provider(client, age_days=30)
    _grant(client, admin, pid, 100)
    _wallet(client, h)
    w2, _ = _wallet(client, h)          # new address on existing account -> cooldown
    r = _withdraw(client, h, w2["wallet_id"], 10)
    assert r.status_code == 200, r.text
    wd = r.json()
    assert wd["risk_decision"] == "EXTENDED_HOLD" and "payout wallet changed recently" in wd["risk_reasons"]
    eligible = datetime.fromisoformat(wd["eligible_at"])
    assert eligible > datetime.now(timezone.utc) + timedelta(hours=23)
    rej = client.post(f"{FIN}/withdrawals/{wd['withdrawal_id']}/reject", headers=admin, json={"reason": "test"})
    assert rej.status_code == 200 and rej.json()["status"] == "REJECTED"
    assert _bal(client, h)["held"] == 0

    _sql("UPDATE providers SET payout_hold = true WHERE id = CAST(:p AS uuid)", p=pid)
    assert _withdraw(client, h, w2["wallet_id"], 10).status_code == 403


def test_self_approval_and_dual_approval(client, admin):
    h, pid = _provider(client, age_days=30)
    _grant(client, admin, pid, 900)
    w, _ = _wallet(client, h)
    wd = _withdraw(client, h, w["wallet_id"], 600).json()  # >= 500 -> dual approval
    assert wd["required_approvals"] == 2
    _eligible(wd["withdrawal_id"])
    a1 = _approve(client, admin, wd["withdrawal_id"])
    assert a1.status_code == 200 and a1.json()["status"] == "SECURITY_HOLD" and len(a1.json()["approvals"]) == 1
    assert _approve(client, admin, wd["withdrawal_id"]).status_code == 409  # same admin twice
    admin2 = _second_admin(client)
    a2 = _approve(client, admin2, wd["withdrawal_id"])
    assert a2.status_code == 200 and a2.json()["status"] == "APPROVED"
    client.post(f"{FIN}/withdrawals/{wd['withdrawal_id']}/reject", headers=admin, json={"reason": "cleanup"})

    # an admin who is also a provider cannot approve their own withdrawal
    client.post(f"{BASE}/provider/onboard", headers=admin2, json={"display_name": "Admin2 farm"})
    pid2 = client.get(f"{BASE}/provider/balances", headers=admin2).json()["provider_id"]
    _sql("UPDATE providers SET created_at = now() - interval '30 days' WHERE id = CAST(:p AS uuid)", p=pid2)
    _grant(client, admin, pid2, 50)
    w2, _ = _wallet(client, admin2)
    own = _withdraw(client, admin2, w2["wallet_id"], 10).json()
    _eligible(own["withdrawal_id"])
    assert _approve(client, admin2, own["withdrawal_id"]).status_code == 403
    assert _approve(client, admin, own["withdrawal_id"]).status_code == 200


# ── payouts ──────────────────────────────────────────────────────────────────

def test_payout_lifecycle_settles_ledger(client, admin):
    h, pid, w, wd = _approved_withdrawal(client, admin, Decimal("20"))
    rb, polls = _settle(client, admin)
    item = next(b for b in rb["broadcast"] if b["withdrawal_id"] == wd["withdrawal_id"])
    assert rb["simulated"] is True and rb["signer"] == "mock" and item["tx_hash"].startswith("0x")
    states = [p for p in polls if wd["withdrawal_id"] in p["confirmed"]]
    assert states, polls
    mine = [x for x in client.get(f"{BASE}/withdrawals/", headers=h).json() if x["withdrawal_id"] == wd["withdrawal_id"]][0]
    assert mine["status"] == "CONFIRMED" and mine["tx_hash"] == item["tx_hash"]
    b = _bal(client, h)
    assert b["held"] == 0 and b["available"] == 20 and b["lifetime_paid"] == 20
    p = next(p for p in client.get(f"{FIN}/payouts", headers=admin).json() if p["withdrawal_id"] == wd["withdrawal_id"])
    assert p["status"] == "CONFIRMED" and p["confirmations"] >= 3 and p["chain_id"] == 84532 and p["simulated"]
    assert p["amount_atomic"] == 20_000_000 and p["recipient"].lower() == w["address"].lower()
    txs = client.get(f"{FIN}/transactions?tx_type=PROVIDER_WITHDRAWAL_SETTLED", headers=admin).json()
    assert any(t["reference_id"] == wd["withdrawal_id"] for t in txs)
    # completed withdrawals cannot be cancelled; the batch never re-pays them
    assert client.post(f"{BASE}/withdrawals/{wd['withdrawal_id']}/cancel", headers=h).status_code == 409
    rb2 = client.post(f"{FIN}/payouts/run-batch", headers=admin).json()
    assert wd["withdrawal_id"] not in [b["withdrawal_id"] for b in rb2["broadcast"]]
    dash = client.get(f"{BASE}/provider/dashboard", headers=h).json()
    assert dash["balances"]["TEST"]["lifetime_paid"] == str(Decimal("20").quantize(Decimal("0.000000000001")))
    assert dash["wallets"][0]["is_verified"] and dash["withdrawals"][0]["status"] == "CONFIRMED"


def test_payout_failure_releases_funds(client, admin):
    h, pid, w, wd = _approved_withdrawal(client, admin, Decimal("15"))
    rb = client.post(f"{FIN}/payouts/run-batch", headers=admin).json()
    item = next(b for b in rb["broadcast"] if b["withdrawal_id"] == wd["withdrawal_id"])
    simulated.FAIL_PAYOUT_IDS.add(item["payout_id"])
    polls = [client.post(f"{FIN}/payouts/poll", headers=admin).json() for _ in range(2)]
    assert any(wd["withdrawal_id"] in p["failed"] for p in polls)
    mine = [x for x in client.get(f"{BASE}/withdrawals/", headers=h).json() if x["withdrawal_id"] == wd["withdrawal_id"]][0]
    assert mine["status"] == "FAILED"
    b = _bal(client, h)
    assert b["held"] == 0 and b["available"] == 30 and b["lifetime_paid"] == 0


def test_payout_kill_switch_blocks_batch(client, admin):
    h, pid, w, wd = _approved_withdrawal(client, admin, Decimal("10"))
    client.post(f"{FIN}/kill-switches", headers=admin, json={"name": "PAUSE_PAYOUTS", "enabled": True})
    assert client.post(f"{FIN}/payouts/run-batch", headers=admin).status_code == 409
    client.post(f"{FIN}/kill-switches", headers=admin, json={"name": "PAUSE_PAYOUTS", "enabled": False})
    rb = client.post(f"{FIN}/payouts/run-batch", headers=admin).json()
    assert wd["withdrawal_id"] in [b["withdrawal_id"] for b in rb["broadcast"]]
    for _ in range(4):
        client.post(f"{FIN}/payouts/poll", headers=admin)


def test_mainnet_withdrawal_is_refused_by_signer(client, admin):
    """Even if a mainnet chain id slipped into an approved withdrawal, the signer refuses it."""
    h, pid, w, wd = _approved_withdrawal(client, admin, Decimal("7"))
    _sql("UPDATE withdrawals SET chain_id = 8453 WHERE id = CAST(:w AS uuid)", w=wd["withdrawal_id"])
    rb = client.post(f"{FIN}/payouts/run-batch", headers=admin).json()
    assert wd["withdrawal_id"] in rb["failed"] and "UNSUPPORTED_CHAIN" in rb["failed"][wd["withdrawal_id"]]
    assert _bal(client, h)["held"] == 0


# ── reconciliation / daily close / treasury ──────────────────────────────────

def _flush_inflight(client, admin):
    """Settle anything other tests left in flight so treasury checks are deterministic."""
    for _ in range(4):
        client.post(f"{FIN}/payouts/poll", headers=admin)


def test_reconciliation_ok_then_tampered_chain_pauses_payouts(client, admin):
    _flush_inflight(client, admin)
    rr = client.post(f"{FIN}/reconcile?book=TEST", headers=admin)
    assert rr.status_code == 200, rr.text
    checks = rr.json()["checks"]
    for name in ("ledger_balanced", "provider_liabilities", "treasury", "withdrawals_vs_payouts"):
        assert checks[name]["ok"] is True, (name, checks[name])
    from app.config import settings
    addr = settings.PAYOUT_HOT_WALLET_ADDRESS.lower()
    _sql("UPDATE sim_chain_balances SET balance_atomic = balance_atomic - 1000000000000 WHERE address = :a", a=addr)
    try:
        bad = client.post(f"{FIN}/reconcile?book=TEST", headers=admin).json()
        assert bad["status"] == "MISMATCH" and bad["checks"]["treasury"]["ok"] is False
        assert Decimal(bad["checks"]["treasury"]["delta"]) == Decimal("-1")
        assert {"kill_switch": "PAUSE_PAYOUTS", "enabled": True} in bad["actions"]
        assert client.get(f"{FIN}/kill-switches", headers=admin).json()["PAUSE_PAYOUTS"]["enabled"] is True
        ev = client.get(f"{FIN}/risk-events", headers=admin).json()
        rec = [e for e in ev if e["category"] == "RECONCILIATION" and "treasury" in e["message"]]
        assert rec
        assert client.post(f"{FIN}/risk-events/{rec[0]['risk_event_id']}/resolve", headers=admin,
                           json={"reason": "test tamper reverted"}).json()["status"] == "RESOLVED"
    finally:
        _sql("UPDATE sim_chain_balances SET balance_atomic = balance_atomic + 1000000000000 WHERE address = :a", a=addr)
    assert client.post(f"{FIN}/reconcile?book=TEST", headers=admin).json()["checks"]["treasury"]["ok"] is True
    runs = client.get(f"{FIN}/reconciliation-runs", headers=admin).json()
    assert len(runs) >= 3


def test_daily_close_and_treasury_summary(client, admin):
    _flush_inflight(client, admin)
    today = datetime.now(timezone.utc).date()
    assert client.post(f"{FIN}/daily-close", headers=admin,
                       json={"date": (today + timedelta(days=1)).isoformat()}).status_code == 400
    day = (today - timedelta(days=random.randint(3000, 30000))).isoformat()  # a day no posting touches
    c1 = client.post(f"{FIN}/daily-close", headers=admin, json={"date": day})
    assert c1.status_code == 200, c1.text
    c2 = client.post(f"{FIN}/daily-close", headers=admin, json={"date": day})
    assert c1.json()["created"] is True and c2.json()["created"] is False
    assert c1.json()["close_id"] == c2.json()["close_id"]
    rep = c1.json()["report"]
    for k in ("opening_treasury", "customer_funding", "gross_workload_revenue", "token_reward_valuation",
              "provider_earnings", "provider_payouts", "network_fees", "payment_fees", "infrastructure_allocation",
              "closing_treasury", "provider_liabilities", "reconciliation_status", "reconciliation_delta"):
        assert k in rep
    assert rep["intraday"] is False and rep["transactions"] == 0

    s = client.get(f"{FIN}/treasury/summary?book=TEST", headers=admin).json()
    assert s["book"] == "TEST" and "Wallet balance != company profit" in s["note"]
    total_assets = Decimal(s["total_assets"])
    liab = Decimal(s["provider_liabilities"]["total"])
    assert Decimal(s["available_company_funds"]) == total_assets - liab - Decimal(s["customer_prepaid"]) \
        - Decimal(s["accrued_costs"]) - Decimal(s["operating_reserve"])
    real = client.get(f"{FIN}/treasury/summary?book=REAL", headers=admin).json()
    assert Decimal(real["total_assets"]) == 0 and "simulated_hot_wallet" not in real
    legacy = client.get(f"{BASE}/admin/revenue", headers=admin).json()
    assert "test_book" in legacy and "net_position" in legacy
