# FARMOS V1 Build Spec — Device Execution Pipeline

## Goal
Build FARMOS's own controlled device execution system. No Acurast dependency for job dispatch/execution.

## Architecture (locked per user decision)

### Device target
- Android first (Stratus C7 = Node #1)
- Backend device-neutral for future iOS

### Job types
- `http_check` — MVP job. Simple HTTP GET/POST, measurable, easy to verify
- Future: `api_request`, `monitor`, `data_transform`

### Payment model
- Separate TEST credits vs REAL USDC ledgers
- Job → device completes → FARMOS verifies → device ledger credited
- No simulated/test earnings recorded as real
- Batch payouts later; no per-micro-job crypto

### Device identity
- Device generates keypair at install
- Private key in Android Keystore
- Public key registered with FARMOS
- Owner approves enrollment
- Server issues node token
- All device requests signed

### Job transport
- V1: HTTP polling/long polling
- `GET /device/jobs/next` — device polls for assigned job
- `POST /device/jobs/{id}/accept` — device accepts
- `POST /device/jobs/{id}/result` — device submits signed result
- Queue independent of transport

## What exists already (backend)
- Device enrollment + heartbeat (done)
- Job/ledger DB models (done)
- Simulator (done — signs enrollment + heartbeat)
- Enums: DeviceStatus, JobStatus, LedgerAccountType, JobEventActorType (done)

## What I'm building now

### Backend additions
1. Job types enum + job payload model
2. Dispatch job to device endpoint
3. Device accepts job endpoint
4. Device submits result endpoint (with signature verification)
5. FARMOS verifies result + updates job status
6. Credit device ledger on verified result
7. `GET /device/jobs/next` — poll for assigned job
8. `GET /device/jobs` — list jobs for device
9. Earnings/ledger summary endpoint

### Device agent (Android-ready, can run as script now)
1. Job loop: poll → accept → execute → submit result → repeat
2. `http_check` executor
3. Signed result submission
4. Heartbeat + job polling in same loop

## Acceptance test flow
```text
1. Register device (simulator or real) → ACTIVE
2. Create http_check job
3. Dispatch job to device
4. Device polls → receives job
5. Device accepts job
6. Device executes http_check
7. Device submits signed result
8. FARMOS verifies result
9. Job marked COMPLETE
10. Device ledger credited
11. Dashboard shows completed job + earnings
```

## Files to create/modify
- `app/db/models/enums.py` — add JobType enum
- `app/api/jobs.py` — add dispatch/accept/result/verify endpoints + device job endpoints
- `app/services/job_executor.py` — job execution + verification service
- `app/services/ledger.py` — device earnings ledger service
- `app/agent/node_agent.py` — device-side job loop (Python now, port to Android later)
- `app/agent/executors/http_check.py` — http_check job executor
