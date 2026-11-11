# Job leases, retries and verification (Phase 2 — spec Sprints 4 & 5)

Covers spec §15 (retry count), §36/§37 (reputation and fraud), §44 (internal CPU/data jobs),
§57.6 (VerificationService), §59 (payloads), §60 (job state machine), §67 (verification
methods), §69–§71 (requeue, never resume an expired lease, leases) and §136 (idempotency).

## Lifecycle

```
CREATED ─dispatch─▶ DISPATCHED ─accept─▶ ACCEPTED ─result─▶ RESULT_SUBMITTED ─▶ VERIFYING ─▶ VERIFIED (credit)
              │ lease issued            │ lease → runtime window              │
              ▼                         ▼                                     ├─▶ INVALID_RESULT ─▶ QUEUED (retry) │ FAILED + dead_lettered
          EXPIRED ◀──── lease timeout ──┘                                     └─▶ FAILED (device reported) ─▶ QUEUED │ dead-letter
              └─▶ QUEUED (retry_count+1)  or  FAILED + dead_lettered (retries exhausted)
```

Every transition writes an immutable `job_events` row that includes the reason, actor, lease id and attempt.

## Leases (`job_leases`)

| field | meaning |
|---|---|
| `id` | lease_id, sent to the device in the poll/accept/dispatch responses |
| `attempt` | `retry_count + 1` when the lease was issued |
| `expires_at`, `grace_seconds` | the lease is live until `expires_at + grace` |
| `status` | `ACTIVE` → `COMPLETED` (result received) / `EXPIRED` |
| `outcome`, `result_hash`, `evidence` | archive of the attempt (VERIFIED / INVALID_RESULT / DEVICE_FAILED / TIMEOUT) |

* A dispatched lease must be accepted within `LEASE_ACCEPT_TIMEOUT_SECONDS` (default 120 s).
* On accept the expiry becomes `now + max_runtime_seconds (or LEASE_DEFAULT_RUNTIME_SECONDS) + LEASE_RUNTIME_BUFFER_SECONDS`.
* `POST /api/v1/jobs/device/jobs/{id}/lease/renew` is signed over `{request_nonce, request_timestamp, job_id, device_id, lease_id}`. It extends the expiry by `LEASE_RENEW_SECONDS`, capped at `LEASE_MAX_TOTAL_SECONDS` after acceptance. Both agents renew every `renew_every_seconds`.
* The **sweep** (`lease_service.sweep_expired`) runs from the `worker-verification` container every 10 s, opportunistically on each device poll, and on demand via `POST /admin/jobs/sweep-leases`. For each expired lease it:
  * frees the device (`RUNNING → IDLE`), and
  * requeues the job with `retry_count + 1`, recording the failing device in `verification_spec.excluded_devices`, or dead-letters it once `retry_count > max_retries`.

  A device that accepted and then went silent takes the timeout reputation penalty. An un-accepted dispatch (the device was offline or paused) takes no penalty.
* **Late results** on an expired lease get a 409 and are never credited. They are not re-verified; this is a deliberate simplification of §71. An expired lease is never resumed (§70).

`job_assignments`, `job_results` and `job_verifications` are 1:1 with a job, so they hold only the current attempt. Earlier attempts live in `job_leases` (`GET /admin/jobs/{id}/attempts`).

## Retries and dead-letter

* `jobs.max_retries` (default `JOB_DEFAULT_MAX_RETRIES=3`, settable per job at creation), `retry_count`, `dead_lettered` and `last_error`.
* `GET /admin/jobs/dead-letter` lists dead-lettered jobs. `POST /admin/jobs/{id}/requeue` moves one back to `QUEUED` with a fresh budget. This requires OWNER or ADMIN.
* Requeued jobs are `QUEUED`. Until the Phase 3 scheduler exists they are re-dispatched with `POST /jobs/dispatch`.

## Idempotency

* `POST /jobs/` accepts an `Idempotency-Key` header. Keys are scoped per user, and a duplicate returns the original job with `idempotent_replay: true`.
* Re-accepting a job you already accepted returns 200 with `idempotent_replay: true`.
* Resubmitting the **same** `result_hash` returns the recorded outcome, even after the job was requeued, because the hash is archived on the lease. A **different** hash for the same attempt returns 409.
* Credit is idempotent per (account, reference_type, job). `POST /jobs/verify/result` on an already verified job returns the outcome without crediting again.
* Accept, renew and result requests consume their nonce, so replaying a request returns 409.

## Verification (`services/verification_service.py`)

| workload | methods | notes |
|---|---|---|
| `checksum`, `json_transform` | HASH + DETERMINISTIC_RECOMPUTE | the server recomputes the exact answer |
| `compression` | HASH + ROUNDTRIP_RECOMPUTE | decompresses and compares the sha256 of the deterministic input, so it does not depend on the zlib version or level |
| `http_check`, `api_request`, `monitor` | SCHEMA + REDUNDANT_SAMPLE | see below |
| canary (any of the above) | KNOWN_ANSWER | the expected answer is stored server-side only |

* `result_hash` must equal `sha256(canonical(result))`, where canonical is sorted keys, compact separators and integral floats written as ints. Both agents send the canonical string as `result_uri`. For deterministic workloads a mismatch is a hard failure. For HTTP workloads it is advisory, because older APKs hashed differently.
* **Redundant execution:** a deterministic sample (`REDUNDANT_SAMPLE_RATE`, hashed from the job id) of HTTP-type primaries is re-run as a shadow job (`role=REDUNDANT`, `parent_job_id`, `max_retries=0`). The shadow goes to a different device that was seen in the last 10 minutes and passes the safety policy, preferring other providers. The primary stays `VERIFYING` until the shadow reports, then:
  * if the status class agrees: VERIFIED with HIGH confidence;
  * if it disagrees: a soft penalty for both devices, and the primary is retried.

  If no second device is available, or the shadow can't finish, verification falls back to schema-only with LOW confidence (`REDUNDANT_FALLBACK_SCHEMA`).
* **Canaries:** `POST /admin/jobs/canary {device_id, workload_type, [url, expected_http_status]}`. A device can't tell a canary from a normal job. A wrong answer applies `REPUTATION_CANARY_FAIL_DELTA` and quarantines the device immediately.
* Verification runs on submit (`VERIFY_ON_SUBMIT=true`). **Test credit is posted only after a PASS**, and only for customer (`PRIMARY`) jobs. Shadow, canary and benchmark jobs never pay.

## Reputation and quarantine (`services/reputation_service.py`)

| event | delta | health event |
|---|---|---|
| verified result | `+1` | – |
| wrong or forged result | `-10` | `VERIFICATION_FAILED` |
| device-reported failure / redundant disagreement | `-3` | `JOB_FAILED` |
| accepted lease timed out | `-3` | `LEASE_EXPIRED` |
| failed canary | `-25` | `CANARY_FAILED` (quarantines immediately) |

The device is auto-quarantined in any of these cases:

* `VERIFICATION_FAILURES_QUARANTINE` (3) failures within `VERIFICATION_FAILURE_WINDOW_SECONDS` (24 h), counted since the last release;
* a failed canary;
* reputation below `REPUTATION_QUARANTINE_BELOW` (15).

The only way out is `POST /admin/devices/{id}/release`, which also lifts reputation to the probation floor (threshold + 5).

## Internal CPU/data workloads (`app/workloads/cpu_data.py`, `CpuDataExecutor.kt`)

| type | payload | result |
|---|---|---|
| `checksum` | `{seed, size_bytes ≤ 8 MiB, rounds ≤ 16}` | `output_sha256` of `det_stream(seed, size)` hashed `rounds` times |
| `compression` | `{seed, size_bytes ≤ 1 MiB, level 1–9}` | zlib of `det_text(seed, size)`: `input_sha256`, `compressed_size`, `compressed_b64` |
| `json_transform` | `{document, ops[≤16]}` with ops `select`, `filter_eq`, `sort_by`, `count`, `sum` (integers only) | `output`, `output_sha256` |

These are classed as heavy workloads, so they are blocked at ORANGE and RED or when the battery is low, per Phase 1 safety rules. Python and Kotlin share known-answer vectors in `test_cpu_data_workloads.py` and `CpuDataExecutorTest.kt`.

## Security notes

* The `VERIFY_DEBUG` logging in `verify_request_signature` was removed. It logged public keys, payloads and signatures. Malformed keys and signatures now fail closed with "Invalid device signature".
* `verification_spec`, which holds the role, hidden expected answers and excluded devices, is never returned to devices.
