# Ledger, wallets, withdrawals & payouts (Phase 4: spec Sprints 8–9)

> **Hard rules for this build:** no real funds, no mainnet transactions, no private keys or
> secrets. Revenue comes only from the **simulated billing source** and lands in the **TEST
> book**. Payouts are signed by a **key-less mock signer** and broadcast to an **in-database
> simulated Base Sepolia chain**. Mainnet chain IDs (1, 8453, 10, 137, 42161) are refused by both
> the signer and the provider. REAL-book withdrawals are disabled (`WITHDRAWAL_REAL_BOOK_ENABLED=false`).

## 1. Double-entry ledger (spec §23, §66, §98–§100, §53.6–7)

* `ledger_transactions`: one row per typed posting (`tx_type`, `book`, unique `idempotency_key`,
  reference, actor, `reverses_transaction_id`). `ledger_entries` hold the debit/credit lines.
* **Books:** `TEST` (asset `TEST_USD`; legacy device test credits use `USD`) and `REAL` (`USDC`).
  A transaction never mixes books, and TEST lines can never carry USDC (or the reverse).
* **Chart of accounts** = spec §99 (1000–6010) + `4045 Infrastructure Recovery` (from the §66
  example) + `9000 Test Credit Issuance` (contra) and `9010 Device Test Earnings` (TEST only).
  Provider-scoped accounts: 2000 Payable (available), 2010 Pending, 2040 Withdrawal Holds.
* **Transaction types** = spec §100 + `TEST_CREDIT`. Each type has an allow-list of accounts;
  unknown types or accounts are rejected. Every transaction must balance per asset.
* **Append-only:** Postgres triggers reject `UPDATE`/`DELETE` on both ledger tables. Corrections
  are `REVERSAL` transactions (one per original, and a reversal can't be reversed). Postings that
  touch treasury cash (1000) mirror chain events and need a compensating transaction instead.
* Every posting writes a `LEDGER_POSTED` audit row. The `provider_balances` projection (one row
  per provider per book) is updated in the same DB transaction; it is never allowed to go negative.

| Event | Posting |
|---|---|
| Revenue confirmed (internal) | Dr 1020 / Cr 2010 provider share, Cr 4045 infra, Cr 4000/4010 FARMOS |
| Revenue confirmed (external/token) | Dr 1030 / Cr 2010, 4045, 4030 |
| Simulated settlement | `CUSTOMER_JOB_CHARGE` or `TOKEN_CONVERSION`: Dr 1000 / Cr clearing (+ sim-chain deposit) |
| Hold elapsed | `PROVIDER_EARNING_AVAILABLE`: Dr 2010 / Cr 2000 |
| Withdrawal requested | `PROVIDER_WITHDRAWAL_HOLD`: Dr 2000 / Cr 2040 |
| Rejected / cancelled / failed | `WITHDRAWAL_RELEASE`: Dr 2040 / Cr 2000 |
| Payout confirmed | `PROVIDER_WITHDRAWAL_SETTLED`: Dr 2040 / Cr 1000 |
| Admin test grant | `TEST_CREDIT`: Dr 1000 / Cr 2000 (simulated faucet deposit into the hot wallet) |
| Device test credit (job result) | `TEST_CREDIT`: Dr 9000 / Cr 9010 (USD) |

## 2. Revenue confirmation & split (spec §25, §29)

`UNCONFIRMED`/`SIMULATED` job economics → `POST /admin/finance/revenue/confirm` → the
`SimulatedBillingSource` returns CONFIRMED (or DISPUTED) → posting above → `revenue_state=CONFIRMED`.
The split policy (`GET/PUT /admin/finance/revenue/split-policy`) has a default (65/15/20) plus
per-adapter or per-workload overrides. Shares must sum to 1, and FARMOS takes the rounding
remainder, so the split always equals gross exactly. After `PROVIDER_EARNINGS_HOLD_HOURS`,
`release-pending` moves earnings to available. Providers on payout hold or with an open risk event stay pending.

## 3. Wallet ownership (spec §21, §22, §27)

1. `POST /wallets/challenge {address, network}` returns an EIP-4361 (SIWE) message with the §21
   statement ("…does not authorize a transfer"), a one-time nonce and a 10-minute expiry.
2. The user signs it with `personal_sign` (EIP-191) in their own wallet.
3. `POST /wallets/verify {challenge_id, signature}` recovers the signer (`eth-account`) and compares it with the address.

Replays return 409. Expired challenges are rejected. Wrong-signer attempts are counted, and the
challenge is burned after `WALLET_MAX_FAILED_ATTEMPTS`. A new payout address on an account that
already has a verified wallet, or a change of default wallet, starts `WALLET_CHANGE_COOLDOWN_HOURS`;
withdrawals can't become eligible before the cooldown ends. `base` (8453) wallets can be verified,
but payouts run only on `PAYOUT_NETWORKS` (Base Sepolia).

## 4. Controlled withdrawals (spec §24, §74, §149)

`POST /withdrawals/` requires an `Idempotency-Key` header and `{wallet_id, amount, book:"TEST"}`.
It checks the following:

* kill switch
* REAL book refused
* provider ACTIVE and not on payout hold
* wallet verified and on a payout network
* minimum/maximum amount and at most 6 decimals
* daily limit (new accounts: lower limit)
* locked available balance

It then runs the §74 risk score:

| Decision | Outcome |
|---|---|
| `STANDARD_HOLD` | 1 h hold |
| `EXTENDED_HOLD` | 72 h hold |
| `MANUAL_REVIEW` | `RISK_REVIEW` status plus a risk event |

Funds are moved to 2040 at request time. Admins approve via `/admin/finance/withdrawals/{id}/approve`:

* Approval is possible only after the hold.
* The requester can't approve their own withdrawal, and the same admin can't approve twice.
* Amounts ≥ `WITHDRAWAL_LARGE_USD` need two approvers.
* The wallet and payout hold are re-checked.

The final approval produces an `approval_hash` that binds withdrawal, provider, recipient, atomic
amount, asset, chain and approvers. Providers can cancel, and admins can reject, until a payout exists.

## 5. Payouts (spec §106–§112)

`app/payouts/` is isolated:

* **`PayoutSigner` interface → `MockSigner`.** No key exists; the "signature" is a hash. It
  enforces every §108 rejection rule: emergency stop, chain or token not allowlisted, mainnet,
  duplicate payout id, wallet unverified, not approved, recipient or amount mismatch, approval-hash
  mismatch, per-transaction and daily limits.
* **`PayoutProvider` interface → `SimulatedPayoutProvider`.** A DB-backed fake chain (hot wallet
  balance plus transactions; finality states SEEN_ONCHAIN → CONFIRMING → CONFIRMED after
  `PAYOUT_CONFIRMATIONS_REQUIRED` polls, or FAILED).

`run-batch` refuses when `PAUSE_PAYOUTS` is on. For each withdrawal it builds the §108 instruction
(USDC 6-dp atomic units, token registry entry), signs it and broadcasts it. Withdrawals are never
completed at broadcast. `poll` settles the ledger on CONFIRMED and releases funds on FAILED.
Retryable rejections (emergency stop, daily limit, hot wallet insufficient) leave the withdrawal APPROVED.

## 6. Reconciliation, daily close, treasury (spec §26, §124, §137–§139)

`POST /admin/finance/reconcile` runs these checks:

* trial balance
* ledger provider liabilities vs `provider_balances`
* treasury 1000 vs simulated hot-wallet balance net of in-flight payouts
* withdrawals vs payouts vs held balances
* external adapter reconciliation status

A failed check opens a risk event. A treasury or withdrawal mismatch also **auto-enables
`PAUSE_PAYOUTS`** (incident runbook step 1).

`POST /admin/finance/daily-close {date}` is idempotent and immutable per (date, book). It refuses
future dates and reports the §138 fields.

`GET /admin/finance/treasury/summary` returns assets, provider liabilities, customer prepaid,
accrued costs, reserve and available company funds, because a wallet balance is not company profit.
Kill switches are `PAUSE_WITHDRAWALS`, `PAUSE_PAYOUTS` and `PAUSE_REVENUE_CONFIRMATION`; every change is audited.

## 7. Provider views

* `POST /provider/onboard`: create an ACTIVE provider and grant the PROVIDER role.
* `GET /provider/balances`: TEST and REAL books, pending, available, held, lifetime.
* `GET /provider/ledger`: only the provider's own lines.
* `GET /provider/dashboard`: now provider-scoped (jobs on the provider's own devices, balances,
  wallets, withdrawals). Previously it summed the global ledger.

The web dashboard has a provider finance panel (wallet verify via `window.ethereum`, withdrawals,
ledger) and an admin treasury panel (summary, approval queue, payouts, reconcile/close, kill switches, risk events).

## 8. Not in this build (follow-ups)

* Real billing sources (Stripe or on-chain receipts) and a REAL-book revenue path.
* A real signer service (HSM/MPC) or RPC provider.
* Treasury tiers (cold, warm, hot), automatic refills and CHAIN_FEE postings.
* EIP-1271 smart-contract wallets.
* 2FA or email confirmation for wallet changes and withdrawals, and user notifications.
* Automatic payout scheduling (a worker/cron calling `run-batch`/`poll`/`release-pending`/`reconcile`).
* Refund/chargeback flows that compensate settled treasury cash.
* Region/sanctions rules.
