# Benchmarks, FarmScore, and hardware safety (Phase 1 / spec Sprint 3)

> Principle (spec §10): **don't make $5 by destroying a $50 device.** Every rule here can only make a node do *less* work, never more.

## Lifecycle (spec §8, §60)

`ENROLLING → BENCHMARKING → ACTIVE`. After enrollment a node stays in `BENCHMARKING` until it submits a benchmark that passes the baseline. To restore the old behavior, set `BENCHMARK_REQUIRED_FOR_ACTIVE=false`. The heartbeat response includes `benchmark_required: true` while this is pending. The Android app and the Python agent then run the suite on their own. They only do this when the device is GREEN and has enough charge.

A benchmark fails the baseline (the node stays `BENCHMARKING`) if any of these is true:
- its FarmScore is below `BENCHMARK_MIN_FARM_SCORE`
- the score jumps more than 40 points from the previous benchmark
- it reports values that aren't physically plausible (possible spoofing)
- the peak temperature during the run went above ORANGE max
- the battery reports a fault

## Benchmark suite `farmos-bench-1` (spec §9, §57.2)

Devices submit **raw measurements**, and the server computes the scores, so a device can never set its own score. The measurements are:
- CPU: SHA-256 hashes per second, single-thread and all cores
- AI: naive fp32 matmul GFLOPS
- Memory: copy MB/s and total RAM
- Storage: sequential write/read MB/s and free space
- Network: latency to `/health`
- Thermal: start, end, and peak °C
- Battery: percentage, charging, and health

Endpoints:
| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/v1/devices/{id}/benchmark/result` | node token + device signature | submit raw results |
| POST | `/api/v1/devices/{id}/benchmark` | OWNER/ADMIN/OPERATIONS | create and dispatch a `benchmark` job (blocked by safety policy when unsafe) |
| GET | `/api/v1/devices/{id}/benchmarks` | user | benchmark history |
| GET | `/api/v1/devices/{id}` | user | now includes `health` and `latest_benchmark` |
| GET/PUT | `/api/v1/admin/farmscore/weights` | admin | weights, normalized to sum to 1; PUT recalculates scores |

**Signing.** `raw_results_json` is the exact string the device produced. The device signs canonical JSON of `{device_id, job_id ("" if none), request_nonce, request_timestamp, results_sha256, suite_version}`. The server checks the SHA-256, the signature against the enrolled key, timestamp freshness, and the nonce (to block replays). Hashing the raw string means differences in how Kotlin and Python format floats can't break verification.

**FarmScore** is the weighted sum of eight components scored 0–100: cpu .20, ai .15, memory .10, storage .10, network .15, thermal .10, power efficiency .10, reliability .10. Weights can be changed by an admin. A missing component counts as 0. Reliability is computed on the server from the last 24 hours of heartbeats. The `raw_results` stored with each benchmark include the weights and component scores, so any score can be reproduced.

Benchmark jobs never create ledger entries. They go straight to `VERIFIED` (method `benchmark_recorded`).

## Thermal / battery governor (spec §10, §62, §63)

| Band | Temp | Effect |
|---|---|---|
| GREEN | < 38 °C | all workloads |
| YELLOW | 38–42 °C | all workloads, concurrency 1, status `THROTTLED` |
| ORANGE | 42–45 °C | light workloads only (`http_check`, `monitor`, `api_request`) |
| RED | > 45 °C | stop compute, status `COOLING` for `THERMAL_COOLDOWN_SECONDS` |
| EMERGENCY | ≥ 50 °C | `emergency_stop` |
| UNKNOWN | no temperature reported | light only |

- **Temperature input.** The hottest reported sensor counts (`temperature_c` or `battery_temperature_c`). Android `thermal_status` sets a minimum: 2 counts as at least ORANGE, and 3 or higher counts as RED.
- **Leaving COOLING.** A device moves `COOLING → IDLE` only when the cooldown window has passed **and** it is back in GREEN. Staying hot extends the window.
- **Battery.** Work pauses below 15%, or below 40% when not charging. Heavy work off the charger needs at least 60%. A full battery that is hot gets light work only, to avoid repeated full-charge/high-temperature cycles. A battery reporting `OVERHEAT` triggers COOLING.
- **Quarantine.** These trigger quarantine automatically:
  - a battery `DEAD`, `OVER_VOLTAGE`, or `UNSPECIFIED_FAILURE` reading
  - 3 RED episodes within an hour

  Quarantine is released only by an explicit admin call (`POST /api/v1/admin/devices/{id}/release`).
- **Enforcement points.** The policy is checked at dispatch (409), at device poll (the job is held, and the response returns `paused`), and at accept (409). This covers coder rule §53.9.
- **Configuration.** Thresholds come from settings and can be overridden at runtime. Overrides can be set per model (`GET/PUT /api/v1/admin/safety/policy`):
  `{"default": {...}, "models": {"Stratus C7": {"green_max": 36, "yellow_max": 40, "orange_max": 43}}}`

### On-device enforcement (defense in depth)
`android-node/.../safety/ThermalGovernor.kt` applies the same thresholds locally, using real `BatteryManager` and `PowerManager` telemetry. It combines its own decision with the server's, and **the stricter one always wins**. `JobLoop` checks it before polling, accepting, or benchmarking, and keeps a local 10-minute cooldown after any RED reading. Server state goes stale after 3 minutes, and then the local governor decides alone. `BenchmarkRunner` won't start unless the device is GREEN, and it aborts above 45 °C.

Heartbeats now carry real telemetry. The earlier hard-coded 80% / 32 °C values are gone. The heartbeat signature covers exactly the fields that were sent, so older clients still verify.
