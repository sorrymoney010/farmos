"""Sprint 6/7 integration: opportunity registry + adapters (internal + simulated
external), adapter kill switch, profitability gate, scheduler auto-dispatch,
double-booking prevention, requeue exclusion, per-job / per-device economics.

No real money, no external network, no credentials: acurast_sim is simulated.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.adapters import acurast_sim
from app.config import settings
from app.tests.integration.test_sprint3_benchmarks_safety import BASE, admin, client  # noqa: F401
from app.tests.integration.test_sprint4_5_leases_verification import (  # noqa: F401
    CHK, _accept, _create, _isolate, _job, _no_random_redundancy, _poll, _ready_node, _sql, _submit,
)
from app.workloads import cpu_data


def _tick(client, admin, *job_ids, dry_run=False):
    r = client.post(f"{BASE}/admin/scheduler/tick", headers=admin, json={"job_ids": list(job_ids), "dry_run": dry_run})
    assert r.status_code == 200, r.text
    return r.json()


def _rep(node, rep):
    _sql("UPDATE devices SET reputation_score = :r WHERE id = CAST(:i AS uuid)", r=rep, i=node.id)


def _opp(client, admin, external_id):
    opps = client.get(f"{BASE}/opportunities/", headers=admin).json()["opportunities"]
    return next(o for o in opps if o["external_id"] == external_id)


def _complete(node, job_id, payload):
    polled = _poll(node)
    assert polled["job"]["job_id"] == job_id
    assert "constraints" not in polled["job"]  # scheduling constraints never reach devices
    assert _accept(node, job_id).status_code == 200
    s = _submit(node, job_id, cpu_data.run_checksum(payload))
    assert s.status_code == 200, s.text
    return s.json()


@pytest.fixture
def adapters_restored(client, admin):
    yield
    for a in ("internal_compute", "acurast_sim"):
        client.post(f"{BASE}/admin/adapters/{a}/enable", headers=admin)
    client.put(f"{BASE}/admin/profitability/policy", headers=admin, json={})


def test_registry_lists_scored_opportunities_and_adapters(client, admin):
    body = client.get(f"{BASE}/opportunities/", headers=admin).json()
    by_ext = {o["external_id"]: o for o in body["opportunities"]}
    assert {"checksum", "http_check", "acu-tee-compute", "acu-token-mining"} <= set(by_ext)
    assert by_ext["checksum"]["adapter_id"] == "internal_compute"
    tee = by_ext["acu-tee-compute"]
    assert tee["simulated"] is True and tee["reference_estimate"]["profitable"] is True
    assert tee["reference_estimate"]["adjusted_gross_usd"] < tee["reference_estimate"]["gross_usd"]  # haircuts
    assert by_ext["acu-token-mining"]["reference_estimate"]["profitable"] is False
    assert body["opportunities"][0]["opportunity_score"] >= body["opportunities"][-1]["opportunity_score"]

    ad = {a["adapter_id"]: a for a in client.get(f"{BASE}/admin/adapters", headers=admin).json()["adapters"]}
    assert ad["acurast_sim"]["simulated"] is True and ad["acurast_sim"]["certification_stage"] == "SANDBOX"
    assert ad["internal_compute"]["enabled"] is True
    h = client.post(f"{BASE}/admin/adapters/acurast_sim/health", headers=admin).json()
    assert h["status"] == "OK" and h["simulated"] is True
    assert client.post(f"{BASE}/opportunities/discover", headers=admin).json()["discovered"]["acurast_sim"] == 2


def test_scheduler_dispatches_to_best_device_and_tracks_economics(client, admin):
    good, weak = _ready_node(client, admin), _ready_node(client, admin)
    _rep(good, 95)
    _rep(weak, 30)
    _isolate(good, weak)
    created = _create(client, admin, "checksum", CHK)
    job = created["job_id"]
    assert created["status"] == "QUEUED" and created["expected_net_usd"] > 0 and created["rejected_reason"] is None

    prev = client.get(f"{BASE}/admin/scheduler/preview/{job}", headers=admin).json()
    assert prev["ranked"][0]["device_id"] == good.id
    dry = _tick(client, admin, job, dry_run=True)
    assert dry["dispatched"][0]["device_id"] == good.id and _job(client, admin, job)["status"] == "QUEUED"

    rep = _tick(client, admin, job)
    assert [d["job_id"] for d in rep["dispatched"]] == [job]
    assert rep["dispatched"][0]["device_id"] == good.id
    attempts = client.get(f"{BASE}/admin/jobs/{job}/attempts", headers=admin).json()
    trail = [(e["from"], e["to"]) for e in attempts["events"]]
    for step in [(None, "CREATED"), ("CREATED", "VALIDATED"), ("VALIDATED", "QUEUED"),
                 ("QUEUED", "MATCHED"), ("MATCHED", "DISPATCHED")]:
        assert step in trail
    est = client.get(f"{BASE}/admin/economics/jobs/{job}", headers=admin).json()
    assert est["status"] == "ESTIMATED" and est["device_id"] == good.id and est["scores"]["final_score"] > 0

    out = _complete(good, job, CHK)
    assert out["status"] == "VERIFIED"
    assert out["verification"]["economics"]["revenue_state"] == "UNCONFIRMED"
    act = client.get(f"{BASE}/admin/economics/jobs/{job}", headers=admin).json()
    assert act["status"] == "ACTUAL" and act["actual"]["gross_usd"] == pytest.approx(0.002)
    assert act["actual"]["net_usd"] == pytest.approx(act["actual"]["gross_usd"] - act["actual"]["cost_usd"])

    devs = {d["device_id"]: d for d in client.get(f"{BASE}/admin/economics/devices", headers=admin).json()["devices"]}
    g = devs[good.id]
    assert g["jobs_completed"] == 1 and g["lifetime_gross_usd"] == pytest.approx(0.002) and g["net_usd"] > 0
    assert devs[weak.id]["jobs_completed"] == 0
    upd = client.put(f"{BASE}/admin/devices/{good.id}/economics", headers=admin,
                     json={"purchase_price_usd": 25, "resale_value_usd": 5})
    assert upd.status_code == 200 and upd.json()["purchase_price_usd"] == 25
    assert upd.json()["purchase_price_is_default"] is False
    summ = client.get(f"{BASE}/admin/economics/summary", headers=admin).json()
    assert summ["by_revenue_state"]["UNCONFIRMED"]["jobs"] >= 1


def test_no_double_booking_sequential_and_concurrent_ticks(client, admin):
    n = _ready_node(client, admin)
    _isolate(n)
    jobs = [_create(client, admin, "checksum", CHK)["job_id"] for _ in range(4)]
    rep = _tick(client, admin, *jobs[:2])
    assert len(rep["dispatched"]) == 1
    waiting = next(iter(rep["waiting"].values()))
    assert "busy" in waiting

    # Free the device, then race two scheduler instances over the remaining jobs.
    first = rep["dispatched"][0]["job_id"]
    assert _complete(n, first, CHK)["status"] == "VERIFIED"
    pending = [j for j in jobs if j != first]

    async def race():
        from app.services import scheduler_service as sched
        eng = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async def one():
                async with AsyncSession(eng, expire_on_commit=False, autoflush=False) as s:
                    return await sched.scheduler_tick(s, job_ids=pending)
            return await asyncio.gather(one(), one(), one())
        finally:
            await eng.dispose()

    reports = asyncio.run(race())
    total = sum(len(r["dispatched"]) for r in reports)
    assert total == 1
    active = _sql("SELECT count(*) FROM job_leases WHERE device_id = CAST(:i AS uuid) AND status = 'ACTIVE'", i=n.id)
    assert active[0][0] == 1
    statuses = sorted(_job(client, admin, j)["status"] for j in pending)
    assert statuses == ["DISPATCHED", "QUEUED", "QUEUED"]


def test_requeued_job_is_rescheduled_away_from_failed_device(client, admin, monkeypatch):
    a, b = _ready_node(client, admin), _ready_node(client, admin)
    _rep(a, 99)
    _rep(b, 20)
    _isolate(a, b)
    job = _create(client, admin, "checksum", CHK, max_retries=3)["job_id"]
    monkeypatch.setattr(settings, "LEASE_ACCEPT_TIMEOUT_SECONDS", -120)
    monkeypatch.setattr(settings, "LEASE_GRACE_SECONDS", 0)
    assert _tick(client, admin, job)["dispatched"][0]["device_id"] == a.id
    client.post(f"{BASE}/admin/jobs/sweep-leases", headers=admin)
    info = _job(client, admin, job)
    assert info["status"] == "QUEUED" and info["retry_count"] == 1
    monkeypatch.setattr(settings, "LEASE_ACCEPT_TIMEOUT_SECONDS", 120)
    rep = _tick(client, admin, job)
    assert rep["dispatched"][0]["device_id"] == b.id  # higher-scored A excluded after its failure


def test_unsafe_devices_are_never_scheduled(client, admin):
    n = _ready_node(client, admin)
    _isolate(n)
    job = _create(client, admin, "checksum", CHK)["job_id"]
    _sql("UPDATE devices SET temperature_state = 'RED' WHERE id = CAST(:i AS uuid)", i=n.id)
    rep = _tick(client, admin, job)
    assert not rep["dispatched"] and "no eligible device" in rep["waiting"][job]
    assert "thermal RED" in client.get(f"{BASE}/admin/scheduler/preview/{job}", headers=admin).json()["excluded"][n.id]
    _sql("UPDATE devices SET temperature_state = 'GREEN', status = 'QUARANTINED' WHERE id = CAST(:i AS uuid)", i=n.id)
    assert not _tick(client, admin, job)["dispatched"]
    _sql("UPDATE devices SET status = 'ACTIVE', last_seen_at = now() - interval '1 hour' WHERE id = CAST(:i AS uuid)", i=n.id)
    assert not _tick(client, admin, job)["dispatched"]
    _sql("UPDATE devices SET last_seen_at = now(), farm_score = 1 WHERE id = CAST(:i AS uuid)", i=n.id)
    j2 = _create(client, admin, "checksum", CHK, constraints={"min_farm_score": 50})["job_id"]
    assert not _tick(client, admin, j2)["dispatched"]


def test_kill_switch_blocks_creation_and_scheduling(client, admin, adapters_restored):
    n = _ready_node(client, admin)
    _isolate(n)
    queued = _create(client, admin, "checksum", CHK)["job_id"]
    r = client.post(f"{BASE}/admin/adapters/internal_compute/disable", headers=admin, json={"reason": "incident drill"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    c = client.post(f"{BASE}/jobs/", headers=admin, json={"workload_type": "checksum", "payload": CHK})
    assert c.status_code == 409 and "kill switch" in c.text
    rep = _tick(client, admin, queued)
    assert not rep["dispatched"] and "kill switch" in rep["waiting"][queued]
    d = client.post(f"{BASE}/jobs/dispatch", headers=admin, json={"job_id": queued, "device_id": n.id})
    assert d.status_code == 409
    client.post(f"{BASE}/admin/adapters/internal_compute/enable", headers=admin)
    assert _tick(client, admin, queued)["dispatched"][0]["job_id"] == queued


def test_unprofitable_work_rejected(client, admin, adapters_restored):
    mining = _opp(client, admin, "acu-token-mining")
    r = client.post(f"{BASE}/opportunities/{mining['opportunity_id']}/jobs", headers=admin, json={"count": 2})
    assert r.status_code == 200, r.text
    assert r.json()["rejected"] == 2
    j = r.json()["created"][0]
    assert j["status"] == "REJECTED" and j["rejected_reason"].startswith("unprofitable")
    econ = client.get(f"{BASE}/admin/economics/jobs/{j['job_id']}", headers=admin).json()
    assert econ["status"] == "REJECTED" and econ["expected"]["net_usd"] < 0

    cheap = _create(client, admin, "checksum", CHK, expected_revenue_usd=0.0000001)
    assert cheap["status"] == "REJECTED"

    bad = client.put(f"{BASE}/admin/profitability/policy", headers=admin, json={"nope": 1})
    assert bad.status_code == 400
    neg = client.put(f"{BASE}/admin/profitability/policy", headers=admin, json={"power_price_kwh": -1})
    assert neg.status_code == 400
    ok = client.put(f"{BASE}/admin/profitability/policy", headers=admin, json={"power_price_kwh": 5000})
    assert ok.status_code == 200 and ok.json()["effective"]["power_price_kwh"] == 5000
    assert _create(client, admin, "checksum", CHK)["status"] == "REJECTED"
    client.put(f"{BASE}/admin/profitability/policy", headers=admin, json={})
    assert _create(client, admin, "checksum", CHK)["status"] == "QUEUED"


def test_simulated_external_adapter_end_to_end_and_reconcile_kill(client, admin, monkeypatch, adapters_restored):
    n = _ready_node(client, admin)
    _isolate(n)
    tee = _opp(client, admin, "acu-tee-compute")
    r = client.post(f"{BASE}/opportunities/{tee['opportunity_id']}/jobs", headers=admin, json={"count": 1})
    assert r.status_code == 200, r.text
    job = r.json()["created"][0]
    assert job["status"] == "QUEUED" and job["adapter_id"] == "acurast_sim"
    rep = _tick(client, admin, job["job_id"])
    assert rep["dispatched"][0]["device_id"] == n.id
    payload = _job(client, admin, job["job_id"])["payload"]
    out = _complete(n, job["job_id"], payload)
    assert out["verification"]["economics"]["revenue_state"] == "SIMULATED"

    rec = client.post(f"{BASE}/admin/adapters/acurast_sim/reconcile", headers=admin).json()
    assert rec["status"] == "MATCHED" and rec["simulated"] is True and rec["expected_asset"] >= 0.25

    monkeypatch.setattr(acurast_sim, "SIM_BALANCE_SHORTFALL", 0.5)
    rec = client.post(f"{BASE}/admin/adapters/acurast_sim/reconcile", headers=admin).json()
    assert rec["status"] == "MISMATCH" and rec["auto_disabled"] is True
    ad = {a["adapter_id"]: a for a in client.get(f"{BASE}/admin/adapters", headers=admin).json()["adapters"]}
    assert ad["acurast_sim"]["enabled"] is False and "reconciliation" in ad["acurast_sim"]["disabled_reason"]
    r = client.post(f"{BASE}/opportunities/{tee['opportunity_id']}/jobs", headers=admin, json={"count": 1})
    assert r.status_code == 409
    # Internal work is unaffected by the external adapter's kill switch.
    assert _create(client, admin, "checksum", CHK)["status"] == "QUEUED"


def test_opportunity_disable_and_deadline_expiry(client, admin):
    n = _ready_node(client, admin)
    _isolate(n)
    chk = _opp(client, admin, "checksum")
    job = _create(client, admin, "checksum", CHK)["job_id"]
    client.post(f"{BASE}/opportunities/{chk['opportunity_id']}/disable", headers=admin, json={"reason": "maintenance"})
    try:
        rep = _tick(client, admin, job)
        assert not rep["dispatched"] and "DISABLED" in rep["waiting"][job]
        c = client.post(f"{BASE}/jobs/", headers=admin, json={"workload_type": "checksum", "payload": CHK})
        assert c.status_code == 409
    finally:
        client.post(f"{BASE}/opportunities/{chk['opportunity_id']}/enable", headers=admin)
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    late = _create(client, admin, "checksum", CHK, deadline_at=past)["job_id"]
    assert late in _tick(client, admin, late)["expired"]
    assert _job(client, admin, late)["status"] == "EXPIRED"
    assert _tick(client, admin, job)["dispatched"][0]["job_id"] == job


def test_priority_desc_and_manual_mode(client, admin):
    n = _ready_node(client, admin)
    _isolate(n)
    low = _create(client, admin, "checksum", CHK, priority=10)["job_id"]
    high = _create(client, admin, "checksum", CHK, priority=500)["job_id"]
    assert _tick(client, admin, low, high)["dispatched"][0]["job_id"] == high
    manual = _create(client, admin, "checksum", CHK, auto_schedule=False)
    assert manual["status"] == "VALIDATED"
    assert manual["job_id"] not in str(_tick(client, admin, manual["job_id"]))
    q = client.get(f"{BASE}/admin/scheduler/queue?limit=500", headers=admin).json()
    assert any(j["job_id"] == low for j in q["queued"])
