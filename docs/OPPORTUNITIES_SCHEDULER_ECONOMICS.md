# Opportunities, profitability, scheduler and economics (Phase 3)

Spec coverage: Sprints 6 and 7. That means §13/§14 (opportunity registry and adapters), §16/§62 (scheduler), §17/§63/§64 (profitability), §65 (actual economics), §57.3/§57.4 (module contracts), §80/§81 (adapter build and DoD), §126 (kill-switch runbook) and §146–148 (final score, urgency, tiers).

> **No money moves in Phase 3.** Internal revenue is marked `UNCONFIRMED` until customer billing lands in Phase 4. The external adapter (`acurast_sim`) is a **simulator**: it makes no network calls and uses no wallet, account or credentials. Its revenue is marked `SIMULATED`.

## Adapters (`backend/app/adapters/`)

`RevenueAdapter` interface: `discover`, `estimate`, `validate`, `build_job`, `verify` (delegates to the Phase 2 VerificationService), `claim`, `reconcile`, `health_check`, `get_constraints` and `get_balance`.

| Adapter | Kind | Stage | Notes |
|---|---|---|---|
| `internal_compute` | INTERNAL | APPROVED | One opportunity per workload, priced in USD from a price list. The customer's `expected_revenue_usd` overrides the list price. |
| `acurast_sim` | EXTERNAL, simulated | SANDBOX | Two opportunities, both valued in ACU. Prices use a price snapshot with a 35% volatility haircut and a 20% liquidity haircut. `acu-tee-compute` is profitable. `acu-token-mining` is designed to be unprofitable. `claim` returns a `SIM-` receipt. `reconcile` compares expected ACU with a simulated balance. |

`system` and `internal-verification` are reserved adapter ids for benchmarks and verification shadows. The kill switch does not apply to them.

### Kill switch

- **Disable an adapter:** `POST /api/v1/admin/adapters/{id}/disable {reason}`. Every disable is audited (`ADAPTER_DISABLED`). While disabled:
  - Job creation returns 409.
  - Manual dispatch returns 409.
  - The scheduler leaves the adapter's queued jobs waiting with the reason "kill switch".
  - In-flight work finishes normally.
- **Auto-disable:** a reconciliation `MISMATCH` disables the adapter automatically (§126). The actor is recorded as SYSTEM.
- **Disable one opportunity:** `POST /api/v1/opportunities/{id}/disable` does the same for a single opportunity.

## Profitability (`services/profitability.py`, pure functions)

- **Gross revenue:**
  - Token reward: `reward_asset × price`, then reduced by `(1 − volatility) × (1 − liquidity)`.
  - Unknown asset price: valued at 0. The engine never assumes a token is worth something.
- **Costs:**
  - electricity: watts × hours × $/kWh. Watts are idle plus (active − idle) × thermal intensity.
  - network: GB × $/GB.
  - api
  - chain fee
  - payout fee: % of gross.
  - infrastructure: per job plus per hour.
  - depreciation: (purchase − resale) / lifetime hours × hours × (1 + thermal intensity).
  - failure risk: P(fail) × gross. P(fail) = base + reputation term + retries term + heat term, capped at 0.95.
- **Rejection:** work with `net ≤ 0` is rejected. A job created for it ends in status `REJECTED` and gets a `REJECTED` economics row with the reason.
- **Opportunity score (§64):** `profit/h × payment_confidence × repeatability × reliability × 1/(1+days/7) × (1−risk) / resource_intensity`.
- **Device fit (§63):** `(0.35 cpu + 0.25 ram + 0.20 net + 0.20 thermal) × (1.0 charging | 0.75)`. Thermal multipliers: GREEN 1, YELLOW .8, ORANGE .35, RED/COOLING 0.
- **Final score (§146):** `opportunity × fit × reputation/100 × urgency × tier`.
  - urgency: deadline under 60 s ×1.25; under 300 s ×1.10.
  - tier: STANDARD 1.0, PRIORITY 1.1, ENTERPRISE 1.2.
- **Policy:** every cost input is in `.env` and can be overridden at runtime with `PUT /api/v1/admin/profitability/policy`. Unknown keys and negative values are rejected.

## Job lifecycle

1. `POST /jobs/` resolves the opportunity. It uses `opportunity_id` if given, otherwise the internal opportunity for the workload.
2. It checks the kill switch and the opportunity status.
3. It estimates the job on a reference device (FarmScore 50, reputation 50).
4. The status trail is then one of:
   - Profitable, auto-scheduled (default): `CREATED → VALIDATED → QUEUED`.
   - Profitable with `auto_schedule=false`: `CREATED → VALIDATED`, then manual dispatch.
   - Unprofitable: `CREATED → REJECTED`.

Request fields:
- `device_id` is now device **affinity**.
- `constraints` accepts `device_affinity`, `min_farm_score`, `ram_mb`, `cpu_pct`, `bandwidth_mbps` and `tier`. Constraints are never sent to devices.
- `deadline_at` sets a deadline for urgency scoring and expiry.

## Scheduler (`services/scheduler_service.py`, worker `python -m app.workers.scheduler_worker`)

Each tick works through `QUEUED` jobs, including jobs requeued by lease expiry. Order is priority **DESC** plus anti-starvation aging (`SCHEDULER_AGING_PER_MINUTE`, capped), then oldest first. For each job:

1. Expire the job if its deadline has passed. Skip it if the adapter is killed, the opportunity is disabled or expired, opportunity capacity is full, or a workload cap is reached.
2. **Hard exclusions.** Profit and urgency can never override these:
   - offline (`DEVICE_ONLINE_SECONDS`)
   - RED/COOLING
   - Phase 1 safety governor (battery, band × workload class, cooldown)
   - non-schedulable status (onboarding, quarantined, locked, …)
   - busy: an ACTIVE lease means capacity is already reserved
   - excluded after a failed attempt (Phase 2 `excluded_devices`)
   - device affinity
   - min FarmScore (opportunity or job)
   - RAM
   - per-provider in-flight cap
3. Estimate per device. Devices where the job would be unprofitable are excluded.
4. Rank by final score. Ties go to the higher FarmScore, then the lower device id, so ranking is deterministic.
5. **Transactional assign.** This runs inside a savepoint:
   - Lock the device and job rows `FOR UPDATE`, re-reading them (`populate_existing`).
   - Re-check that the job is still QUEUED and the device has no ACTIVE lease.
   - Create the assignment and lease.
   - Write events `QUEUED→MATCHED` (the reason lists the score breakdown) and `MATCHED→DISPATCHED`.
   - Write the economics estimate.
   - On a conflict, the next device is tried. Concurrent schedulers cannot double-book a device (covered by tests).

Admin endpoints:
- `POST /admin/scheduler/tick {job_ids?, dry_run?}`
- `GET /admin/scheduler/queue`
- `GET /admin/scheduler/preview/{job_id}`: ranked candidates plus an exclusion reason for every other device.

## Economics

- **`job_economics`:** one row per job. Status is `ESTIMATED` (at creation and at dispatch) or `REJECTED`. It becomes `ACTUAL` after verification **PASS** (§65, PRIMARY jobs only, idempotent):
  - Runtime is measured from lease accept to close.
  - Network cost uses the result's `bytes_transferred`.
  - Actual costs carry no failure term.
- **`device_economics`:** running per-device P&L (gross, electricity, network, infrastructure, depreciation, other, net, jobs, runtime). Purchase price, resale value, maintenance and watts are set with `PUT /admin/devices/{id}/economics`. ROI and payback days are computed on read.
- **Endpoints:** `GET /admin/economics/jobs[?status=]`, `/admin/economics/jobs/{id}`, `/admin/economics/devices` and `/admin/economics/summary?hours=`.

## Dashboard

`web/app/RevenueEngine.js` appears under the main dashboard after Refresh. It has these panels:
- adapters with Kill/Enable/Reconcile buttons
- ranked opportunities with gross → adjusted, cost, net and score
- the scheduler queue with Dry run / Run tick and wait reasons
- the 24 h economics summary
- device P&L
- latest job economics

## Not in Phase 3 (follow-ups)

- Region rules: devices have no region field.
- Monitoring endpoint allowlist: only an http(s) scheme check is done.
- A real Acurast integration: needs owner-approved credentials and a wallet, so it is out of scope.
- Double-entry ledger, billing that confirms revenue, and claims/withdrawals: Phase 4.
