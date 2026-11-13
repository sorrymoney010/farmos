# FARMOS crypto phone network — active product direction

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
