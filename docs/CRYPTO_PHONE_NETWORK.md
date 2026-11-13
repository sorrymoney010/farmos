# FARMOS native phone network

The active core is our own Android worker, direct FARMOS enrollment, our own data
jobs and verification, followed by crypto funding. Acurast is a design reference
and an optional legacy integration, not a required account, app or service.

# Historical initial fleet phase (superseded below)

FARMOS is an Acurast-style useful-compute phone network with crypto compensation.
The intended upgrade is better onboarding, hardware protection, workload selection,
verification and transparent net rewards. These advantages require measurement;
this release does not establish that FARMOS outperforms another network.

## This phase

Built from main 04c79c7 independently of the pending Stripe/developer PR #8.
The `/crypto-fleet` page and authenticated
`GET /api/v1/acurast/fleet-readiness` endpoint expose the existing signed processor
bridge. Pagination is bounded to 200 records. Responses omit SSIDs and raw
check-in payloads. The page keeps its access token in component memory only.

Readiness observations require enabled registration, fresh signed telemetry,
plausible known battery/temperature, temperature below 42 C, battery at least 20%,
charging, and Wi-Fi or USB. These are conservative advisory pilot thresholds, not
Acurast eligibility rules, not proof of unmetered connectivity, and not remote
hardware enforcement. Future timestamps over one minute and check-ins older
than two configured refresh intervals fail the checks. Unknown/invalid telemetry
fails closed. Network eligibility and rewards remain explicitly unverified.

There is no fabricated reward balance: confirmed_rewards is null until a real
verifier is connected. This phase does not create network jobs, attest hardware,
mine coins, move funds, or provision an official processor app.

## Build sequence toward real crypto earning

1. Connect one owner-controlled eligible phone; observe its signed check-ins.
2. Implement a read-only, chain-specific finalized reward verifier. Validate
   network identity, finalized block, recipient, asset, amount and event identity;
   deduplicate events and distinguish job rewards from ordinary wallet transfers.
3. Prove a funded useful-compute job and matching crypto receipt. Self-funded
   testing must remain labeled as testing, not independent customer revenue.
4. Build FARMOS's own crypto-funded job supply and settlement around the existing
   scheduler/verification. External networks are optional sources of work; the
   Acurast bridge is an initial integration, not FARMOS's product identity.
5. Measure costs, receipt timing and net device earnings before expanding fleets.

Stripe is outside this core path. PR #8 remains separate; reusable access/security
improvements should be reviewed and selectively brought into core before public
customer job access is exposed. No funds or live settings were changed here.

## Verification and remaining gates

130 backend unit tests passed on this main-based branch (15 new); Next.js
production build passed with `/crypto-fleet`. Counts differ from PR #8 because
this branch intentionally excludes its customer billing implementation/tests.
No physical phone, reward receipt, backend deployment or browser visual QA was
verified. The existing Next.js 14.2.5 dependency emitted a security advisory during
installation; upgrade and verify it before deploying the web application.
Android/physical-device validation remains open. Existing public bridge status
routes also need a privacy/compatibility review before public deployment; this
phase's new endpoint is authenticated and does not expose SSIDs.


## Native enrollment and data pilot — current phase

The /crypto-fleet page now reads native /admin/fleet devices. It creates one-time
claim codes through /devices/claim-code, lets the owner select a device and edit
JSON document/ops, creates a local json_transform pilot job, dispatches it, and
retrieves the independently verified result. No Acurast calls are made by this
flow. Existing bridge code is retained as an optional integration.

Pilot jobs have pilot_mode=true, zero expected gross revenue and manual dispatch.
Only OWNER/ADMIN can create jobs. Local CPU/data workloads alone are allowed in
pilot mode; the server still validates payloads. Dispatch checks device safety,
fresh heartbeat and active-lease capacity. Auto-scheduling is off. Pilot jobs
receive neither test earnings credits nor revenue confirmation. Costs may still
be measured. A result hash plus deterministic verification is evidence of work,
not evidence of payment. The UI uses zero retries for its one-device pilot.

GET /jobs/{job_id}/verified-result returns the stored JSON and hash only after
PASSED verification, respecting job ownership and staff roles. Customer job
isolation from PR #8 was selectively reused without its Stripe code.

Android enrollment now saves and validates the entered server origin before the
claim exchange, forbids changing servers while enrolled, disables enrollment HTTP
redirects and removes logs containing token prefixes or enrollment response bodies.
The expired hard-coded tunnel is replaced with farmos.invalid: supply a real
HTTPS server origin; the placeholder cannot be enrolled against.

### What the owner will do once APK and backend are available

1. Install FARMOS Node on compatible Android (current project minSdk 31 / Android 12).
2. Open /crypto-fleet on the deployed FARMOS dashboard with an owner/admin token.
3. Create a pairing code. In FARMOS Node enter the deployed API HTTPS origin and
   that code, then tap Enroll. No admin token goes on the phone.
4. Keep the app open and phone cool/powered; wait for benchmark and heartbeats.
5. Refresh phones, select it, edit the JSON job, run the unfunded pilot, and check
   until independent verification passes. Save the result/hash as test evidence.

No APK was built or physical device connected in this phase. Gradle download has
been blocked in this environment. The backend is not deployed by these changes.
Web dependencies retain the previously documented upgrade gate. No live income,
crypto payment or 24-hour background worker behavior is claimed. Crypto-funded
job acceptance/settlement and actual phone validation remain subsequent work.

Current validation: 147 backend unit tests passed, including reused access-control
regressions and new pilot/result/zero-credit tests. The frontend production build
passed. These tests do not substitute for PostgreSQL end-to-end or Android tests.
