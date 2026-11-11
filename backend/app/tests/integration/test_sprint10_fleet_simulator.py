"""Sprint 10 / DoD item 19: the fleet simulator drives >= 100 fake nodes end to end
against the real backend + database and every safety / money invariant holds.

Runs in-process (httpx ASGITransport + app lifespan) on the shared test DB with
``isolate=True`` so pre-existing test devices do not take simulated work.
"""

from __future__ import annotations

import asyncio

from app.simulator import fleet


def test_allocation_is_deterministic_and_covers_every_profile():
    import random

    a = [p.name for p in fleet.allocate(100, random.Random(7))]
    b = [p.name for p in fleet.allocate(100, random.Random(7))]
    assert a == b and len(a) == 100
    assert {p.name for p in fleet.PROFILES} == set(a)


def test_fleet_of_100_nodes_end_to_end():
    cfg = fleet.Config(nodes=100, jobs=200, external_share=0.1, isolate=True, timeout_s=240, seed=2026)
    r = asyncio.run(fleet.run(cfg))
    print(fleet.format_report(r))
    inv = r["invariants"]
    assert inv["double_booking_device_samples"] == 0 and inv["double_booking_job_samples"] == 0
    assert inv["duplicate_ledger_postings"] == 0
    assert inv["unsafe_node_dispatches"] == 0
    assert inv["ledger_balanced"] is True
    assert r["jobs_outcome"]["unfinished"] == 0
    assert r["jobs_outcome"]["verified"] >= 0.95 * r["jobs_created"]["total"]
    assert r["jobs_created"]["external_simulated"] == 20
    assert inv["jobs_credited"] == r["jobs_outcome"]["verified"]  # credited once each, only when verified
    prof = r["profiles"]
    assert sum(p["nodes"] for p in prof.values()) == 100
    assert prof["fraudulent"].get("result_VERIFIED", 0) == 0          # tampered results never verify
    assert prof["hot_red"]["received"] == 0 and prof["low_battery"]["received"] == 0  # protect hardware
    assert r["quarantine"]["honest_quarantined"] == 0
    fin = r["finance"]
    assert fin["book"] == "TEST" and fin["adapter_reconcile"] == "MATCHED"
    assert fin["confirmed"] >= r["jobs_outcome"]["verified"]
    assert r["passed"] is True
