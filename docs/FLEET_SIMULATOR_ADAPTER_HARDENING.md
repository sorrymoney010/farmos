# Sprint 10 — Fleet simulator, load test, external adapter hardening, fresh-DB migrations

Completes V1.1 (Definition of Done item 19: "simulator handles at least 100 fake nodes").
Spec references: §79 (node simulator), §77 (E2E / chaos), §126 (adapter failure runbook),
§127/§128 (adapter + economic certification), §139 (kill switches).

**Money stays in test mode.** Revenue is confirmed into the `TEST` book from the
simulated billing source, adapters are simulated (`acurast_sim` has no network, no wallet,
no keys), payouts remain the simulated chain. Nothing here moves real funds.

## 1. Fleet simulator (`backend/app/simulator/fleet.py`)

```bash
cd backend
# in-process (httpx ASGITransport + app lifespan) against DATABASE_URL
python -m app.simulator.fleet --nodes 100 --jobs 500 --json /tmp/fleet.json
# shared/dev DB with other devices: keep simulated work on simulated nodes
python -m app.simulator.fleet --nodes 100 --jobs 200 --isolate
# against a running server (timing = the server's own settings)
python -m app.simulator.fleet --base-url http://localhost:8000 --database-url postgresql+asyncpg://...
```

Exit code 0 = PASS. The simulator refuses to run with `APP_ENV=production`.

Each fake node uses the real signed device protocol: EC P-256 key, admin enrollment
token, signed enroll, signed heartbeats with telemetry, signed benchmark, poll, signed
accept, local execution of the real CPU/data workload, signed result hash.

| Profile (share) | Behaviour | Expectation |
|---|---|---|
| healthy (50%) | 28–36 °C, 60–100% battery, charging | does most work |
| warm_yellow (10%) | 38.5–41.5 °C | THROTTLED, still works |
| slow (10%) | +0.2–0.6 s completion latency, lower FarmScore | works, slower |
| weak_farmscore (5%) | benchmark ×0.25–0.45 | ranked lower |
| hot_red (6%) | 46–48 °C | COOLING, **must receive no work** |
| low_battery (6%) | 8–13%, discharging | **must receive no work** |
| disconnecting (6%) | vanishes holding a lease (60%/job) | lease expires, job requeued |
| fraudulent (7%) | tampers every result hash field | verification fails → requeue → quarantine |

Run phases: enroll, then activate (GREEN heartbeat, benchmark, profile heartbeat), then a
mixed job load (checksum / compression / json_transform plus 10% simulated-external
`acu-tee-compute`). Then work rounds: 2 **concurrent** scheduler ticks per round to stress
locking; every live node heartbeats and does poll/accept/execute/submit; lease sweep.
Finally: TEST-book revenue confirm, release-pending, adapter reconcile, treasury
reconcile, trial balance.

**Invariants** (checked straight from the DB):
- no device ever above `MAX_CONCURRENT_JOBS_PER_DEVICE` ACTIVE leases, and no job with
  two ACTIVE leases. Sampled twice per round, before and after the nodes work.
- no job has more than one posting of any ledger transaction type (credit, confirm,
  release), whichever device ran it. `jobs_credited` equals the verified jobs.
- hot / low-battery nodes received **zero** dispatches after their unsafe heartbeat.
- TEST trial balance is balanced, with zero unbalanced transactions.
- no honest node quarantined, no unfinished jobs, and at least 95% of jobs verified
  (`--min-success`).

In-process runs compress timings so the whole lifecycle fits in one run:
- accept timeout `max(4 s, nodes/20)`
- device-online window `max(20 s, nodes/10)`
- earnings hold 0 h

Settings are restored afterwards.

## 2. Load-test results (this box, in-process, Postgres 16 + fakeredis, pooled engine)

Fresh DB built with `alembic upgrade head`, `APP_ENV=development`, pool 20+20.

**100 nodes / 500 jobs (50 simulated-external)** — PASS

| metric | value |
|---|---|
| work phase | 41.0 s (16 rounds), total 64.4 s incl. enroll 1.1 s / activate 1.8 s / finance |
| throughput | **12.2 verified jobs/s**, 63.7 HTTP req/s overall |
| jobs | 500/500 VERIFIED, 0 dead-lettered, 0 unfinished, 27 requeues |
| verification | 521 submissions, 21 rejected (all fraudulent), failure rate 4.03% |
| quarantine | 7/7 fraudulent nodes quarantined, 0 honest nodes |
| safety | hot/low-battery nodes: 0 dispatches |
| double-booking | 0 device-over-cap samples, 0 multi-leased jobs |
| ledger | 500 credited once each, 500 confirmed + 500 released (TEST), trial balance balanced, 0 unbalanced tx |
| reconcile | adapter MATCHED, treasury OK |
| HTTP p50 / p95 | poll 114/218 ms, accept 315/682 ms, submit 714/1317 ms, tick 149/2626 ms, 0 errors |

**1000 nodes / 2000 jobs (200 simulated-external), spec §79 upper bound** — PASS

| metric | value |
|---|---|
| work phase | 228.9 s (8 rounds), total 333 s (enroll 9.5 s, activate 18.4 s) |
| throughput | **8.7 verified jobs/s**, 78 HTTP req/s |
| jobs | 2000/2000 VERIFIED, 0 dead-lettered, 0 unfinished, 201 requeues |
| verification | 2140 submissions, 140 rejected (all fraudulent), failure rate 6.54% |
| quarantine | 0/70 fraud nodes reached the 3-failure threshold (≈2 jobs each); 0 honest nodes |
| invariants | 0 double-booking samples, 0 duplicate postings, 0 unsafe dispatches, ledger balanced |
| HTTP p50 / p95 | poll 164/334 ms, submit 1185/1478 ms, **tick 3.0 s / 25.5 s**, revenue confirm ≈10 s per 500 rows |

At 1000 devices the scheduler tick is the bottleneck: ranking is O(jobs × devices) in
Python. A first run that kept the 4 s accept window dead-lettered 11% of jobs because the
simulator's own rounds outlasted it. Accept windows now scale with fleet size, and
scheduler pre-filtering is listed as a follow-up.

## 3. Bugs the load test found (fixed in this PR)

1. **Scheduler lock convoy.** Two concurrent ticks over 500 jobs × 100 devices took
   **~125 s per tick**. Blocking `FOR UPDATE` plus stale busy counts made the losing tick
   walk all ~100 ranked devices per job. Leases then expired before nodes could accept,
   and 86 jobs were dead-lettered. The fix in `scheduler_service._assign`:
   - `SKIP LOCKED` on the job and device locks;
   - a JOB_GONE vs DEVICE_UNAVAILABLE distinction;
   - devices taken elsewhere are marked busy for the rest of the tick;
   - at most 8 device attempts per job.

   Ticks are now 149 ms p50.
2. **Redis pool exhaustion.** `ConnectionPool(max_connections=20)` raised
   `Too many connections` under concurrent enrollments. It is now a
   `BlockingConnectionPool` that waits up to `REDIS_POOL_TIMEOUT_SECONDS`.
3. **First-request race on a fresh DB.** Concurrent requests both inserted the
   `revenue_adapters` / `opportunities` seed rows (unique violations → 500s). The inserts
   now run in savepoints, and a unique violation means another request already created
   the row.

## 4. External adapter hardening (`backend/app/adapters/guard.py`)

- **Bounded, contained calls.** `discover`, `health_check`, `reconcile`, `claim` and
  `get_constraints` go through `guard.call`. It applies an `asyncio.wait_for` timeout
  (`ADAPTER_CALL_TIMEOUT_SECONDS`), turns any exception into `AdapterCallError`, and
  tracks call/error/consecutive-failure counts plus the last error on the adapter record.
- **Circuit breaker.** `ADAPTER_CIRCUIT_FAILURE_THRESHOLD` consecutive failures open the
  circuit for `ADAPTER_CIRCUIT_COOLDOWN_SECONDS`, and calls are short-circuited while it
  is open. An EXTERNAL adapter is also **auto-disabled** by the SYSTEM actor (§126 step 1)
  and audited (`ADAPTER_CIRCUIT_OPENED`). An admin re-enable resets the breaker.
- **Untrusted discovery output.** Each `OpportunitySpec` is validated: types, string
  lengths, finite numbers in range, object fields, payload template valid for the
  workload, deduplicated external ids, at most `ADAPTER_MAX_OPPORTUNITIES`. Invalid specs
  are rejected with reasons in `health_detail.last_discovery` and never stored. A
  discovery that returns only garbage counts as a failure. A failing discovery keeps the
  existing catalogue unchanged. `validate()` exceptions mark the opportunity UNSCORABLE.
- **Granular switches (§139).** `POST /admin/adapters/{id}/switches` takes
  `{name: DISABLE_DISCOVERY|DISABLE_SCHEDULING|DISABLE_CLAIMS, enabled}` and is audited.
- **Global `PAUSE_EXTERNAL_ADAPTERS`.** It lives in the platform kill switches
  (`/admin/finance/kill-switches`) and stops every EXTERNAL adapter: discovery,
  scheduling, claims and release of its earnings. Internal work continues.
- **§126 earnings guard.** While an external adapter is disabled, globally paused, or its
  last reconciliation is `MISMATCH`/`ERROR`, its revenue is not confirmed and its
  CONFIRMED earnings are **not** released to AVAILABLE. They become releasable only after
  a clean (`MATCHED`) reconciliation. A reconcile call that fails is recorded as `ERROR`,
  not silently ignored.
- **Simulated claims.** `POST /admin/adapters/{id}/claim` uses a `finalized_at`
  watermark, so work is never claimed twice. A failed claim does not advance the
  watermark. Claims are refused when the adapter is disabled, `DISABLE_CLAIMS` is on, the
  global pause is on, or reconciliation is not clean.
- **Certification (§127/§128).** `POST /admin/adapters/{id}/certification` (OWNER/ADMIN)
  moves the adapter one stage forward at a time; backward moves are allowed. Leaving
  ECONOMIC_REVIEW requires the full §127 checklist and the §128 economic fields. Adapters
  that need custody, or whose terms forbid automation or mobile use, cannot be certified.
  CANARY and later require a MATCHED reconciliation. **Simulated adapters can never pass
  SANDBOX.** History is kept on the record and audited.
- `GET /admin/adapters` now shows switches, call stats, circuit state, certification and
  last claim.
- Chaos hooks for tests: `acurast_sim.FAULT_MODE[op] = "error" | "timeout" | "garbage"`.

Migration `0007_adapter_hardening` adds the new `revenue_adapters` columns idempotently.

## 5. Migrations: fresh `alembic upgrade head` now works

- **`0001`** no longer needs `uuid-ossp`. Defaults use `gen_random_uuid()`, which is
  built into PostgreSQL ≥ 13. `pgcrypto` is created only on servers older than 13.
- **`0003b_baseline_tables` (new)**. The core job/ledger/wallet tables were only ever
  created by `create_all`, so a fresh `upgrade head` failed at 0004 (`NoSuchTableError:
  jobs`). 0003b creates them with `IF NOT EXISTS` in their phase-1 shapes; 0004–0006
  then evolve them as before. It also aligns `devices.farm_id` to VARCHAR(100) and adds
  the indexes the models declare. Its downgrade is intentionally a no-op: these tables
  hold real data in every existing environment.
- **Existing DBs stamped at 0006** pick up only 0007 (0003b is an ancestor of 0006 and is
  not re-run).

Verified on this box:
- fresh DB: `upgrade head` → `downgrade -1` → `upgrade head`.
- schema diff against the models leaves only benign differences: columns wider in the
  DB, and unique constraints vs unique indexes.
- the **full backend suite passes on the migration-built DB**.
- a `create_all` DB stamped at 0006 upgrades cleanly to 0007.

## 6. Tests

- `tests/unit/test_adapter_guard.py`: spec validation, dedupe, caps.
- `tests/integration/test_sprint10_adapter_hardening.py`:
  - timeouts → circuit → auto-disable → re-enable;
  - discovery errors and garbage;
  - switches and global pause;
  - claims with no double counting;
  - §126 release guard;
  - reconcile outage;
  - certification rules.
- `tests/integration/test_sprint10_fleet_simulator.py`: 100 nodes / 200 jobs end to end
  against the real app + DB, asserting every invariant (~60 s).
