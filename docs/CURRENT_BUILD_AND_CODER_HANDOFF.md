# FARMOS current build and coder handoff

Reviewed November 11, 2026. Scope: repository/documentation review; no deployment, payment activation or full test rerun.

## Baseline and evidence

- Repository: https://github.com/sorrymoney010/farmos
- Main reviewed: `04c79c703c2e76925991afec62dc41eec19ccf33`.
- PR #8 head reviewed: `2344b426f7ab11993750c053cab3a0fe69db4c89`; open at review time.
- PR #7 is merged into main, satisfying PR #8's stated dependency. PR #8 already targets main. Refresh its mergeability and diff against current main before merge.
- This review inspected selected source paths and existing reports. It is not a comprehensive security audit or independent verification of reported test results.

## What exists

| Area | Evidence/status |
| --- | --- |
| Device enrollment, Android node, benchmarks and thermal protection | Source present; physical-device deployment not verified here |
| Leases, retries, independent verification | Merged; docs/LEASES_AND_VERIFICATION.md |
| Opportunities, economics and scheduler | Merged; docs/OPPORTUNITIES_SCHEDULER_ECONOMICS.md |
| Ledger, wallets and withdrawals | Merged; TEST-book billing and simulated payout chain; docs/LEDGER_WALLETS_WITHDRAWALS.md |
| Fleet simulation and adapter hardening | Sprint 10 merged; docs/FLEET_SIMULATOR_ADAPTER_HARDENING.md |
| Customer deployments, SDK, quotes and prepaid Stripe billing | PR #8 only; not yet on main |
| Real customer receipts, production uptime, real provider payouts | Not verified |

Sprint 10's report records 500/500 verified jobs with 100 simulated nodes and 2000/2000 with 1000 simulated nodes. These are author-reported simulation results. The commit reports 192 backend tests; PR #8 reports 236 passing tests and a successful web build. None were rerun during this documentation review. At 1000 nodes the report identifies scheduler ranking as a bottleneck (p95 tick 25.5 seconds).

The `acurast_sim` adapter is explicitly simulated. Separate Acurast bridge/service files do not establish that the scheduler has a certified, paying external adapter.

## PR #8 review and required coder work

Recommendation: continue isolated test-mode development; do not treat this PR as production launch approval. Do not merge it as part of this documentation update.

1. **Customer isolation — confirmed source finding.** `backend/app/api/jobs.py` permits CUSTOMER on list/detail. The list query selects all jobs and detail does not scope to customer ownership. This file is unchanged by PR #8. Apply ownership checks to every customer-accessible read, result and mutation path, retaining explicit administrative access. Acceptance: customer A cannot list, read or change B's jobs/results; unauthorized identifiers return a consistent denial.
2. **Legacy job creation and billing — review gate.** The same legacy create endpoint permits CUSTOMER and does not use the new deployment reservation path. Trace whether a customer can bypass prepaid funding, quotes or URL validation. Restrict this route or funnel customer work through the same policy/billing service. Acceptance: no customer dispatch without an authorized reservation; no bypass using old endpoints.
3. **Network egress — confirmed validation gap.** PR #8 `services/developer/deployments.py:check_url` checks hostname syntax and literal IPs, not resolved destination addresses. `deliver_pending` calls it before an ordinary HTTP client connection. Protect both device job execution and server webhook delivery against DNS rebinding and private/metadata destinations at connection time; validate redirects and IPv4/IPv6. Acceptance: tests reject private DNS resolutions, rebinding and unsafe redirects while public destinations work.
4. **Payment reconciliation — disclosed incomplete scope.** PR #8 records Stripe customer funding, but its follow-ups leave settlement, balance transactions and fees incomplete. Reconcile processor clearing, net cash, fees, refunds and disputes before reporting withdrawable real earnings. Real USD processor receipts and USDC settlement must have explicit currency/conversion treatment; do not assume a dollar receipt is an on-chain USDC balance.
5. **Real payouts — absent from shipped provider.** Implement and validate a real provider separately before enabling REAL-book withdrawals. Keep simulation isolated. Require receipt/transaction evidence and reconciliation; never count pending balances as received cash.
6. **Operational evidence.** Update against current main, run migrations on fresh and upgraded databases, backend suite and frontend build, then run one physical Android device end to end. Record exact commit, commands, results and device/OS. Test interruption, overheating, retries and duplicate billing events.

## Money-making pilot acceptance

Use one existing workload and one consenting owner-controlled Android device first. A monitoring pilot is an option only after egress fixes. An actual customer or documented external payer must fund the work; self-funded testing is not customer revenue.

Required evidence: customer order and authorized price -> verified payment -> reserved job -> physical-device execution -> independent result verification -> delivered result -> exactly-once charge -> reconciled receipt and measured costs. Report gross receipts, refunds, processor/network/infrastructure costs, device energy/depreciation assumptions and net contribution separately. Label estimates and simulations clearly.

Do not expand the fleet until repeatable demand and positive measured economics justify it. No fake engagement, unauthorized proxy work, account farming or deposit-to-unlock schemes.

## Coder execution order

1. Bring PR #8 onto current main and resolve review gates 1–3 with regression tests.
2. Validate test billing, refunds/disputes, duplicate events, migration upgrade paths and reservation release; attach evidence to the PR.
3. Prove the physical-device test workflow. Keep live credentials and funds off during this stage.
4. Complete settlement/accounting and real payout work as separate reviewed changes.
5. Prepare a concrete limited paid-pilot launch checklist with actual customer demand, operating costs and rollback controls for owner review.

Preserve existing code and specifications. Use this file for current status; historical specs remain design references, not proof of completion. Return small reviewable changes and evidence rather than another full-platform rebuild.
