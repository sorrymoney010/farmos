# FARMOS Developer Platform + Prepaid Billing (Sprint 16)

FARMOS's **own task supply**: developers/customers deploy work to the phone fleet
directly — no dependency on Acurast or any external marketplace. Every run
becomes an ordinary FARMOS job, so the scheduler, leases, verification,
economics and all hardware-protection rules (thermal / battery / charging /
lease limits) apply unchanged.

> **Money mode:** TEST by default. With no Stripe key, or with an `sk_test_` key,
> everything posts to the **TEST** ledger book and no real money moves. Live keys
> are refused unless the owner explicitly enables live mode (see "Going live").

## Concepts

| Thing | What it is |
|---|---|
| Customer | one per user (`POST /developer/account`); grants the DEVELOPER role |
| API key | `fos_test_<prefix>_<secret>`; HMAC-SHA256 hash stored, shown once, scoped, revocable, optional expiry |
| Quote | price for one run of a spec, from the profitability engine; TTL 15 min; bound to the spec hash |
| Deployment | workload + payload + resources + schedule + result destination at a locked price |
| Run | one Job + one prepaid reservation (`customer_job_charges`) |
| Top-up | Stripe Checkout Session (or PaymentIntent); credited **only** by the signed webhook |

Scopes: `jobs:read`, `jobs:write`, `billing:read`, `billing:write`, `webhooks:write`.
Key management (`/developer/keys`) always needs the dashboard JWT, so a leaked
key cannot mint or revoke keys.

## Supported workloads

| workload_type | payload | notes |
|---|---|---|
| `http_check` | `{url, method: GET\|HEAD, timeout<=30}` | public https only |
| `monitor` | `{targets: [url, ...]}` (≤10) | public https only, redundant-sampled verification |
| `checksum` | `{seed, size_bytes<=8MiB, algorithm, rounds<=16}` | deterministic re-check |
| `compression` | `{seed, size_bytes<=1MiB}` | deterministic re-check |
| `json_transform` | `{document, ops}` (≤256 KiB) | deterministic re-check |

**URL policy (protects providers' home networks):** https only; no
localhost / `.local` / `.internal` / single-label hosts; no private, loopback,
link-local (incl. 169.254.169.254), multicast or reserved IP literals; no
credentials in URLs; ports 80/443/8080/8443 only; GET/HEAD only, no custom
headers or bodies.

Resources: `cpu_pct`, `ram_mb`, `bandwidth_mbps`, `tier` (STANDARD/PRIORITY/ENTERPRISE), `min_farm_score`.

Schedule: `{}` = run once now; recurring = `{interval_seconds>=60, max_runs and/or end_at, start_at?}`.
Recurring deployments must be bounded (max_runs or end_at). A worker that fell
behind resumes the cadence (no backfill bursts).

Destination: `{"type": "api"}` (poll `/runs`) or `{"type": "webhook", "url": "https://..."}`
(needs `webhooks:write`). Webhooks are signed `FARMOS-Signature: t=<ts>,v1=<hmac_sha256(secret, "<ts>.<body>")>`,
carry `FARMOS-Event-Id` (idempotency id) and `FARMOS-Event-Type`, and retry with
exponential backoff (30 s · 2^n, 6 attempts) with a delivery log. Events:
`job.created`, `job.verified` (includes the result), `job.failed`, `balance.low`.
The secret is shown once at creation. `farmos_sdk.verify_webhook_signature()` verifies it.

## Pricing (spec §170-§173)

```
fixed     = device costs from profitability.estimate_job() at gross 0 (power, wear,
            network, infrastructure), runtime scaled by payload size
variable  = payout-fee share + failure probability
verify    = PRICING_VERIFICATION_USD (+ redundant-sample share for monitoring)
risk      = fixed * PRICING_RISK_PREMIUM
price     = max((fixed + verify + risk) * (1+margin) * tier * urgency / (1 - variable), MIN_PRICE)
floor     : contribution margin >= PRICING_MIN_CONTRIBUTION_MARGIN, and estimate_job(price) profitable
maximum   = price * (1 + PRICING_RESERVE_BUFFER)   (reserved per run; only `price` is charged)
FCU       = runtime_min * cpu_pct/25 * max(1, ram_mb/256)   (informational)
```

Response: `quote_id, estimated_cost_usd, maximum_cost_usd, estimated_runtime_seconds, fcu, expires_at, breakdown`.
Deploy with `quote_id` (must match the spec, unexpired) or `max_price_usd` (auto-accept up to that price).

## Money flow (double-entry, idempotent)

| Step | Trigger | Ledger |
|---|---|---|
| Fund | Stripe `checkout.session.completed` (paid) / `async_payment_succeeded` / `payment_intent.succeeded` | `CUSTOMER_FUNDING` Dr 1050 Customer Funds Clearing / Cr 2020 Customer Prepaid Liability[customer] (`funding:<topup>`) |
| Reserve | run created | none — `RESERVED` row; available = 2020 balance − open reservations (customer row locked) |
| Charge | PRIMARY verification PASSED | `CUSTOMER_JOB_CHARGE` Dr 2020[customer] / Cr 1020 Revenue Clearing (`custcharge:<job>`) |
| Revenue | same step (savepoint) | `JOB_REVENUE_CONFIRMED` Dr 1020 / Cr 2010 provider, 4045 infra, 4000/4010 FARMOS (`revenue:<job>`), source `customer_prepaid` — no simulated settlement |
| Release | run ends without verified work (expired, rejected, cancelled, dead-lettered) | none — `RELEASED` |
| Refund | `charge.refunded` (cumulative amount, delta posted) | `REFUND` Dr 2020 / Cr 1050 |
| Dispute | `charge.dispute.created` | risk event + customer ON_HOLD |

Customers are never charged for unverified work. Revenue for deployment jobs
is confirmed only from the prepaid charge (a real billing event), never from
the simulated billing source and never from a frontend redirect. If
`PAUSE_REVENUE_CONFIRMATION` is on, the charge still happens and the normal
confirm-revenue batch confirms later through the same customer source.
Insufficient funds → `402` on create; recurring deployments pause
(`PAUSED_FUNDS`) and auto-resume after a top-up.

## Stripe webhook — `POST /api/v1/webhooks/stripe`

1. raw body + `Stripe-Signature` (`t=`, one or more `v1=`), HMAC-SHA256 with
   `STRIPE_WEBHOOK_SECRET`, 300 s tolerance (replay protection);
2. insert `stripe_webhook_events` (unique `stripe_event_id`, event_type, livemode,
   received_at, processed_at, processing_status, related customer/top-up,
   payload_hash) — duplicates return 200 without re-processing (one credit, §154);
3. process inside the same transaction; amount, currency, livemode and customer
   must match the FARMOS top-up, otherwise `REJECTED` + `STRIPE_MISMATCH` risk event;
4. a processing error rolls back and returns 500 so Stripe retries.

Statuses: `PROCESSED`, `DUPLICATE` (second event for an already funded top-up),
`IGNORED` (unhandled type / unpaid session / unknown top-up), `REJECTED`.

Reconciliation (`GET /admin/developer/reconcile?book=TEST`, §137): succeeded
top-ups ⇄ `CUSTOMER_FUNDING` transactions, and
`2020 balance == funded − refunded − charged`.

## API summary (`/api/v1/developer`)

| Method | Path | Scope |
|---|---|---|
| POST/GET | `/account` | JWT / any |
| GET | `/billing/status` | public |
| POST/GET/DELETE | `/keys`, `/keys/{id}` | JWT only |
| POST | `/quotes` | jobs:read |
| POST | `/deployments` (Idempotency-Key) | jobs:write (+webhooks:write for webhook) |
| GET | `/deployments`, `/deployments/{id}`, `/deployments/{id}/runs`, `/deployments/{id}/webhook-deliveries`, `/jobs/{job_id}` | jobs:read |
| POST | `/deployments/{id}/cancel` | jobs:write |
| GET | `/balance`, `/ledger`, `/topups` | billing:read |
| POST | `/topups` (Idempotency-Key) | billing:write |

Admin (`/api/v1/admin/developer`): `customers`, `customers/{id}/hold|release`,
`reconcile`, `tick`, `stripe-events`.

The scheduler worker runs the deployments tick (create due runs, resume funded
deployments, release failed reservations, deliver webhooks) before each
scheduler tick.

## SDK + CLI (`sdk/python`)

```bash
pip install -e sdk/python
export FARMOS_API_URL=https://api.farmos.example FARMOS_API_KEY=fos_test_...
farmos balance
farmos topup 20                       # prints a Stripe Checkout URL
farmos quote http_check '{"url":"https://example.com"}' --runs 288
farmos deploy http_check '{"url":"https://example.com"}' --every 300 --runs 288 --max-price 0.01
farmos runs <deployment_id>
farmos cancel <deployment_id>
```

Dashboard: `web/app/developer` (`/developer`) — keys, deployments, results, balance, top-up.

## Local testing with Stripe test mode (optional)

Tests never touch the network (Stripe is mocked with `httpx.MockTransport`
and webhooks are signed in-process). To try against a real **test-mode**
Stripe account: set `STRIPE_SECRET_KEY=sk_test_...`, run
`stripe listen --forward-to localhost:8000/api/v1/webhooks/stripe`, put the
printed `whsec_...` into `STRIPE_WEBHOOK_SECRET`, and pay with card
`4242 4242 4242 4242`.

## Going live (owner only)

1. In the Stripe dashboard (owner's own account): activate the account; create a
   **restricted key** (`rk_live_...`) with write access to Checkout Sessions and
   PaymentIntents only (or use `sk_live_...`).
2. Add a webhook endpoint `https://<api-host>/api/v1/webhooks/stripe` with events
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed`, `checkout.session.expired`,
   `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.refunded`,
   `charge.dispute.created`; copy its signing secret (`whsec_...`).
3. Set in the production secret store (never in git): `STRIPE_SECRET_KEY`,
   `STRIPE_WEBHOOK_SECRET`, `STRIPE_LIVE_MODE_ENABLED=true`, `APP_ENV=production`,
   `STRIPE_CHECKOUT_SUCCESS_URL` / `STRIPE_CHECKOUT_CANCEL_URL` (public dashboard URLs),
   and `NEXT_PUBLIC_API_URL` for the web app.
4. Live funding posts to the **REAL** book. Provider payouts from the REAL book
   still require a real payout provider (the repo only ships the simulated one),
   and the existing withdrawal approvals/limits apply.
5. Run `GET /admin/developer/reconcile?book=REAL` daily; compare with Stripe balance
   reports (Stripe→bank payouts are not yet ingested; see follow-ups).

## Customer job access hardening — November 11, 2026

Customer JWT requests to `GET /api/v1/jobs/` and `GET /api/v1/jobs/{id}` now
see only jobs owned by that user. Ownership is filtered in SQL before pagination
and detail lookup. Another customer's job, an unowned job and an unknown ID all
return the same 404 response. OWNER, ADMIN and OPERATIONS retain fleet visibility,
including staff accounts that also hold the CUSTOMER role.

`POST /api/v1/jobs/` is now OWNER/ADMIN only. Customers must submit through
`POST /api/v1/developer/deployments` so quotes, policy checks and prepaid
reservations cannot be bypassed through the legacy endpoint. This intentionally
changes legacy customer clients; switch them to the deployment API.

Validation on this change: 153 backend unit tests passed, including 11 new
HTTP/SQL authorization regressions in `tests/unit/test_customer_job_access.py`.
Those regressions use an isolated SQLite database, actual role checks, and an
overridden identity dependency. They do not replace PostgreSQL integration,
JWT authentication, migration, physical-device or production validation.

Still open before a paid launch: connection-time egress protection (devices and
server webhooks), processor settlement/fee reconciliation, a real payout provider,
and full integration/physical-device evidence. Live billing and withdrawals have
not been enabled by this change.

## Public network egress — November 12, 2026

Backend customer webhooks and HTTP/API/monitor job execution now use
`PublicHTTPTransport`. It resolves the destination once per request, rejects any
non-public answer (including mixed public/private sets and IPv6 transition
ranges), and connects to the approved literal IP. The original hostname is used
for Host, TLS SNI and certificate verification. Each request uses a new pool to
avoid sharing TLS connections between different names on the same IP. Proxy
environment variables and redirects are disabled. DNS has a five-second limit;
raw responses are limited to 1 MiB. The transport requests identity encoding and
rejects compressed responses to avoid decompression bypassing that bound.

Admission and execution now require HTTPS on port 443 or 8443; the previous
`CUSTOMER_WEBHOOK_ALLOW_HTTP` setting no longer relaxes destination admission.
Local/intranet monitoring and endpoints requiring redirects or compression are
not supported by this public-workload boundary. Test-only injected mock webhook
transports still make no network calls.

Android HttpCheckExecutor now uses PublicEgress: validates literal addresses,
checks every DNS answer at OkHttp's connection resolution, disables redirects and
proxies, and isolates its connection pool. Preview reads are bounded to 1024
bytes instead of loading an entire response. No enrollment/control-plane URLs
are changed. Public-address validation does not authorize scanning a destination;
customers remain responsible for permission to monitor their targets.

Validation: 186 backend unit tests passed (33 added in this phase); targeted
undefined-name/import lint and whitespace checks passed. New JVM tests cover
private/literal/mixed DNS, later rebinding, public IPv4/IPv6, URL credentials,
ports and redirect settings. Android test execution was attempted but blocked
because services.gradle.org could not resolve while downloading Gradle 8.9.
Android compilation, physical-device behavior, live TLS network checks and the
PostgreSQL integration suite are not verified by these results. Do not treat
this as production or paid-launch approval. Next: obtain Android build/device
evidence and complete processor settlement/fee reconciliation and real payouts.
