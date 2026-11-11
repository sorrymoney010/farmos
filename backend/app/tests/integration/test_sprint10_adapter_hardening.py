"""Sprint 10 integration: external adapter hardening (spec §126, §127, §139).

Timeouts, contained failures, circuit breaker + auto-disable, garbage-output
validation, granular switches, global PAUSE_EXTERNAL_ADAPTERS, simulated claims
without double counting, and the §126 rule that unreconciled external earnings
never become AVAILABLE. Everything is simulated: no network, no wallet, no money.
"""

from __future__ import annotations

import pytest

from app.adapters import acurast_sim
from app.config import settings
from app.tests.integration.test_sprint3_benchmarks_safety import BASE, admin, client  # noqa: F401
from app.tests.integration.test_sprint4_5_leases_verification import (  # noqa: F401
    CHK, _create, _isolate, _job, _no_random_redundancy, _ready_node, _sql,
)
from app.tests.integration.test_sprint6_7_opportunities_scheduler import _complete, _opp, _tick

FIN = f"{BASE}/admin/finance"
AD = f"{BASE}/admin/adapters"


def _adapter(client, admin, aid="acurast_sim"):
    return {a["adapter_id"]: a for a in client.get(AD, headers=admin).json()["adapters"]}[aid]


@pytest.fixture
def hardened(client, admin, monkeypatch):
    monkeypatch.setattr(acurast_sim, "FAULT_MODE", {})
    monkeypatch.setattr(acurast_sim, "SIM_BALANCE_SHORTFALL", 0.0)

    def reset():
        for sw in ("DISABLE_DISCOVERY", "DISABLE_SCHEDULING", "DISABLE_CLAIMS"):
            client.post(f"{AD}/acurast_sim/switches", headers=admin, json={"name": sw, "enabled": False})
        for n in ("PAUSE_EXTERNAL_ADAPTERS", "PAUSE_REVENUE_CONFIRMATION"):
            client.post(f"{FIN}/kill-switches", headers=admin, json={"name": n, "enabled": False})
        client.post(f"{AD}/acurast_sim/enable", headers=admin)

    reset()
    yield
    acurast_sim.FAULT_MODE.clear()
    reset()
    client.post(f"{AD}/acurast_sim/reconcile", headers=admin)  # leave a clean reconciliation behind


def _acu_job(client, admin):
    n = _ready_node(client, admin)
    _sql("UPDATE devices SET reputation_score = 99 WHERE id = CAST(:i AS uuid)", i=n.id)
    _isolate(n)
    tee = _opp(client, admin, "acu-tee-compute")
    r = client.post(f"{BASE}/opportunities/{tee['opportunity_id']}/jobs", headers=admin, json={"count": 1})
    assert r.status_code == 200, r.text
    job = r.json()["created"][0]["job_id"]
    assert _tick(client, admin, job)["dispatched"][0]["device_id"] == n.id
    out = _complete(n, job, _job(client, admin, job)["payload"])
    assert out["status"] == "VERIFIED", out
    return job


def test_timeouts_open_circuit_and_auto_disable_external_adapter(client, admin, hardened, monkeypatch):
    monkeypatch.setattr(settings, "ADAPTER_CALL_TIMEOUT_SECONDS", 0.2)
    acurast_sim.FAULT_MODE["health_check"] = "timeout"
    before = _adapter(client, admin)["calls"]
    for i in range(settings.ADAPTER_CIRCUIT_FAILURE_THRESHOLD):
        h = client.post(f"{AD}/acurast_sim/health", headers=admin)
        assert h.status_code == 200, h.text  # an adapter failure never crashes admin
        assert h.json()["status"] == "ERROR" and "timeout" in h.json()["error"]
    a = _adapter(client, admin)
    assert a["circuit_open"] is True and a["calls"]["consecutive_failures"] == settings.ADAPTER_CIRCUIT_FAILURE_THRESHOLD
    assert a["calls"]["errors"] - before["errors"] == settings.ADAPTER_CIRCUIT_FAILURE_THRESHOLD
    assert a["enabled"] is False and "circuit opened" in a["disabled_reason"]  # spec §126 step 1
    # open circuit: short-circuited, the adapter is not called at all
    h = client.post(f"{AD}/acurast_sim/health", headers=admin).json()
    assert h["status"] == "CIRCUIT_OPEN"
    assert _adapter(client, admin)["calls"]["total"] == a["calls"]["total"]
    # disabled adapter: no new jobs, internal work unaffected
    tee = _opp(client, admin, "acu-tee-compute")
    assert client.post(f"{BASE}/opportunities/{tee['opportunity_id']}/jobs", headers=admin,
                       json={"count": 1}).status_code == 409
    assert _create(client, admin, "checksum", CHK)["status"] == "QUEUED"
    # admin re-enable closes the breaker; healthy again once the fault clears
    acurast_sim.FAULT_MODE.clear()
    assert client.post(f"{AD}/acurast_sim/enable", headers=admin).json()["circuit_open"] is False
    assert client.post(f"{AD}/acurast_sim/health", headers=admin).json()["status"] == "OK"
    assert _adapter(client, admin)["calls"]["consecutive_failures"] == 0


def test_discovery_errors_and_garbage_are_contained(client, admin, hardened):
    acurast_sim.FAULT_MODE["discover"] = "error"
    r = client.post(f"{AD}/acurast_sim/discover", headers=admin)
    assert r.status_code == 200 and r.json()["refreshed"] == 0
    assert "simulated discover outage" in r.json()["last_discovery_error"]
    assert client.post(f"{BASE}/opportunities/discover", headers=admin).status_code == 200
    assert _opp(client, admin, "acu-tee-compute")["status"] == "ACTIVE"  # existing catalogue retained

    acurast_sim.FAULT_MODE["discover"] = "garbage"
    body = client.post(f"{AD}/acurast_sim/discover", headers=admin).json()
    assert body["refreshed"] == 0
    rej = body["last_discovery"]["rejected"]
    assert len(rej) == 2 and body["last_discovery"]["rejected_count"] == 2
    errs = " ".join(" ".join(x["errors"]) for x in rej)
    assert "external_id" in errs and "thermal_intensity" in errs and "payment_confidence" in errs
    assert not any(o["external_id"] == "acu-bad" for o in
                   client.get(f"{BASE}/opportunities/", headers=admin).json()["opportunities"])

    acurast_sim.FAULT_MODE.clear()
    assert client.post(f"{AD}/acurast_sim/discover", headers=admin).json()["refreshed"] == 2


def test_granular_switches_and_global_pause(client, admin, hardened):
    tee = _opp(client, admin, "acu-tee-compute")
    mk = lambda: client.post(f"{BASE}/opportunities/{tee['opportunity_id']}/jobs", headers=admin,  # noqa: E731
                             json={"count": 1})
    assert client.post(f"{AD}/acurast_sim/switches", headers=admin,
                       json={"name": "DISABLE_EVERYTHING", "enabled": True}).status_code == 400

    a = client.post(f"{AD}/acurast_sim/switches", headers=admin,
                    json={"name": "DISABLE_SCHEDULING", "enabled": True, "reason": "test"}).json()
    assert a["switches"]["DISABLE_SCHEDULING"] is True and a["enabled"] is True
    assert mk().status_code == 409
    client.post(f"{AD}/acurast_sim/switches", headers=admin, json={"name": "DISABLE_SCHEDULING", "enabled": False})
    assert mk().status_code == 200

    client.post(f"{AD}/acurast_sim/switches", headers=admin, json={"name": "DISABLE_DISCOVERY", "enabled": True})
    assert client.post(f"{AD}/acurast_sim/discover", headers=admin).json()["refreshed"] == 0
    client.post(f"{AD}/acurast_sim/switches", headers=admin, json={"name": "DISABLE_DISCOVERY", "enabled": False})

    client.post(f"{AD}/acurast_sim/switches", headers=admin, json={"name": "DISABLE_CLAIMS", "enabled": True})
    r = client.post(f"{AD}/acurast_sim/claim", headers=admin)
    assert r.status_code == 409 and "DISABLE_CLAIMS" in r.json()["detail"]
    client.post(f"{AD}/acurast_sim/switches", headers=admin, json={"name": "DISABLE_CLAIMS", "enabled": False})

    # global external pause: every EXTERNAL adapter stops, internal work continues
    client.post(f"{FIN}/kill-switches", headers=admin, json={"name": "PAUSE_EXTERNAL_ADAPTERS", "enabled": True})
    assert mk().status_code == 409
    assert client.post(f"{AD}/acurast_sim/discover", headers=admin).json()["refreshed"] == 0
    assert client.post(f"{AD}/acurast_sim/claim", headers=admin).status_code == 409
    assert _create(client, admin, "checksum", CHK)["status"] == "QUEUED"
    client.post(f"{FIN}/kill-switches", headers=admin, json={"name": "PAUSE_EXTERNAL_ADAPTERS", "enabled": False})
    assert mk().status_code == 200


def test_simulated_claim_is_never_double_counted(client, admin, hardened):
    _acu_job(client, admin)
    assert client.post(f"{AD}/acurast_sim/reconcile", headers=admin).json()["status"] == "MATCHED"
    c = client.post(f"{AD}/acurast_sim/claim", headers=admin)
    assert c.status_code == 200, c.text
    c = c.json()
    assert c["status"] == "SIMULATED_CLAIMED" and c["simulated"] is True and c["jobs"] >= 1
    assert c["receipt_ref"].startswith("SIM-")
    assert client.post(f"{AD}/acurast_sim/claim", headers=admin).json()["status"] == "NOTHING_TO_CLAIM"
    # a failing claim is contained and does not advance the watermark
    _acu_job(client, admin)
    acurast_sim.FAULT_MODE["claim"] = "error"
    assert client.post(f"{AD}/acurast_sim/claim", headers=admin).json()["status"] == "ERROR"
    acurast_sim.FAULT_MODE.clear()
    again = client.post(f"{AD}/acurast_sim/claim", headers=admin).json()
    assert again["status"] == "SIMULATED_CLAIMED" and again["jobs"] == 1


def test_unreconciled_external_earnings_never_become_available(client, admin, hardened, monkeypatch):
    job = _acu_job(client, admin)
    rep = client.post(f"{FIN}/revenue/confirm", headers=admin, json={"job_ids": [job]}).json()
    assert [c["job_id"] for c in rep["confirmed"]] == [job], rep
    _sql("UPDATE job_economics SET confirmed_at = now() - interval '25 hours' WHERE job_id = CAST(:j AS uuid)", j=job)

    monkeypatch.setattr(acurast_sim, "SIM_BALANCE_SHORTFALL", 0.5)
    assert client.post(f"{AD}/acurast_sim/reconcile", headers=admin).json()["status"] == "MISMATCH"
    rel = client.post(f"{FIN}/revenue/release-pending", headers=admin).json()
    assert job not in [r["job_id"] for r in rel["released"]]
    assert "spec §126" in rel["held"][job]
    # new external revenue is not confirmed while the mismatch stands
    client.post(f"{AD}/acurast_sim/enable", headers=admin)  # still MISMATCH -> blocked
    job2 = _acu_job(client, admin)
    rep2 = client.post(f"{FIN}/revenue/confirm", headers=admin, json={"job_ids": [job2]}).json()
    assert rep2["confirmed"] == [] and "reconciliation" in rep2["skipped"][job2]

    # counterparty pays in full -> clean reconciliation -> release proceeds (restore only after reconciliation)
    monkeypatch.setattr(acurast_sim, "SIM_BALANCE_SHORTFALL", 0.0)
    assert client.post(f"{AD}/acurast_sim/reconcile", headers=admin).json()["status"] == "MATCHED"
    rel = client.post(f"{FIN}/revenue/release-pending", headers=admin).json()
    assert job in [r["job_id"] for r in rel["released"]]


def test_reconcile_outage_blocks_release(client, admin, hardened):
    acurast_sim.FAULT_MODE["reconcile"] = "error"
    out = client.post(f"{AD}/acurast_sim/reconcile", headers=admin).json()
    assert out["status"] == "ERROR" and "outage" in out["detail"]
    assert _adapter(client, admin)["last_reconcile"]["status"] == "ERROR"
    acurast_sim.FAULT_MODE.clear()
    assert client.post(f"{AD}/acurast_sim/reconcile", headers=admin).json()["status"] == "MATCHED"


def test_certification_pipeline_rules(client, admin, hardened):
    url = f"{AD}/acurast_sim/certification"
    r = client.post(url, headers=admin, json={"stage": "CANARY"})
    assert r.status_code == 409
    probs = " ".join(r.json()["detail"]["problems"])
    assert "simulated adapters cannot advance beyond SANDBOX" in probs and "checklist incomplete" in probs
    assert client.post(url, headers=admin, json={"stage": "APPROVED"}).status_code == 409
    assert client.post(url, headers=admin, json={"stage": "BOGUS"}).status_code == 409
    assert client.post(url, headers=admin, json={"stage": "SANDBOX", "checklist": {"nope": True}}).status_code == 400
    # step back to review, then forward again only with checklist + economics
    assert client.post(url, headers=admin, json={"stage": "ECONOMIC_REVIEW"}).json()["certification_stage"] == \
        "ECONOMIC_REVIEW"
    assert client.post(url, headers=admin, json={"stage": "SANDBOX"}).status_code == 409
    from app.adapters import guard
    checklist = {k: False for k in guard.CERTIFICATION_CHECKLIST}
    checklist.update(terms_permit_automation=True, terms_permit_mobile_devices=True, rewards_verifiable=True,
                     workload_isolated=True, adapter_disableable_without_deploy=True, safe_failure_behavior=True)
    econ = {k: 0.0 for k in guard.ECONOMIC_FIELDS}
    ok = client.post(url, headers=admin, json={"stage": "SANDBOX", "checklist": checklist, "economics": econ,
                                               "notes": "simulated sandbox"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["certification_stage"] == "SANDBOX" and body["certification"]["history"][-1]["to"] == "SANDBOX"
