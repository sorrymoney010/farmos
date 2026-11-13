from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List
import secrets


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Application
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_WORKERS: int = 1

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://farmos:farmos@localhost:5432/farmos"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_POOL_TIMEOUT: int = 30

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_POOL_SIZE: int = 10
    REDIS_POOL_TIMEOUT_SECONDS: float = 10.0   # wait for a free pooled connection before erroring

    # Security
    SECRET_KEY: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        min_length=32,
    )
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    PASSWORD_MIN_LENGTH: int = 12
    DEVICE_ENROLLMENT_TOKEN_EXPIRE_MINUTES: int = 15
    NODE_REQUEST_MAX_SKEW_SECONDS: int = 300
    NODE_TOKEN_EXPIRE_HOURS: int = 24

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # Staging remote enrollment admin convenience header.
    # This is NOT a user bearer token; it is only for the standalone staging admin page.
    STAGING_ENROLL_ADMIN_SECRET: str = Field(
        default_factory=lambda: secrets.token_urlsafe(24),
        min_length=16,
    )
    PUBLIC_API_BASE_URL: str = ""
    STAGING_ADMIN_USER_ID: str = ""

    # Acurast Processor Management Backend compatibility. The official
    # processor controls its own wallet, attestation, workload execution and
    # rewards; FARMOS only receives signed management check-ins.
    ACURAST_CHECKIN_REFRESH_SECONDS: int = 1800

    # Observability
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"
    METRICS_ENABLED: bool = True

    # Admin Seeding
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "ChangeMe123!"
    ADMIN_FULL_NAME: str = "FARMOS Admin"

    # Feature Flags
    SIMULATOR_ENABLED: bool = True

    # Wireless short claim/pairing codes.
    # When True (staging/dev ONLY), claim code "123" is accepted as a fixed convenience
    # code that issues a real enrollment token for STAGING_ADMIN_USER_ID.
    # Must remain False in production — never hardcode 123 as a prod path.
    STAGING_CLAIM_CODES: bool = False
    CLAIM_CODE_LENGTH: int = 6
    CLAIM_CODE_EXPIRE_MINUTES: int = 15


    # ── Sprint 3: Benchmarks / FarmScore (spec §9, §57.2) ───────────────────
    # Suite version every score references; FarmScore must be reproducible.
    BENCHMARK_SUITE_VERSION: str = "farmos-bench-1"
    # Spec §9 recommended initial weights. Admin can override at runtime via
    # PUT /api/v1/admin/farmscore/weights (stored in system_settings).
    FARMSCORE_WEIGHTS: dict[str, float] = {
        "cpu": 0.20,
        "ai": 0.15,
        "memory": 0.10,
        "storage": 0.10,
        "network": 0.15,
        "thermal": 0.10,
        "power_efficiency": 0.10,
        "reliability": 0.10,
    }
    # Spec §8: node becomes ACTIVE only after passing baseline health checks.
    BENCHMARK_REQUIRED_FOR_ACTIVE: bool = True
    BENCHMARK_MIN_FARM_SCORE: float = 10.0

    # ── Thermal + hardware safety (spec §10, §76) — protect hardware ────────
    THERMAL_GREEN_MAX_C: float = 38.0   # < 38 normal
    THERMAL_YELLOW_MAX_C: float = 42.0  # 38–42 reduce concurrency
    THERMAL_ORANGE_MAX_C: float = 45.0  # 42–45 light workloads only; > 45 RED stop + cool
    THERMAL_EMERGENCY_C: float = 50.0   # emergency stop of everything on the node
    THERMAL_COOLDOWN_SECONDS: int = 600  # per-device cooldown window after RED
    THERMAL_RED_EVENTS_QUARANTINE: int = 3  # RED events within window -> quarantine
    THERMAL_RED_WINDOW_SECONDS: int = 3600
    # Battery policy (spec §10 + §68 runtime contract min_battery_pct=40)
    BATTERY_MIN_PCT_DISCHARGING: float = 40.0
    BATTERY_CRITICAL_PCT: float = 15.0
    BATTERY_HEAVY_MIN_PCT_DISCHARGING: float = 60.0  # heavy work off-charger
    # Per-model threshold overrides, e.g. {"SM-G991U": {"green_max": 37, ...}}
    THERMAL_MODEL_OVERRIDES: dict[str, dict[str, float]] = {}


    # ── Phase 2: job leases (spec §71) ───────────────────────────────────────
    LEASE_ACCEPT_TIMEOUT_SECONDS: int = 120    # DISPATCHED -> must accept within
    LEASE_DEFAULT_RUNTIME_SECONDS: int = 300   # when job has no max_runtime_seconds
    LEASE_RUNTIME_BUFFER_SECONDS: int = 60
    LEASE_GRACE_SECONDS: int = 30
    LEASE_RENEW_SECONDS: int = 120
    LEASE_MAX_TOTAL_SECONDS: int = 3600        # hard cap from first accept
    JOB_DEFAULT_MAX_RETRIES: int = 3
    LEASE_SWEEP_ON_POLL: bool = True           # opportunistic sweep without a worker

    # ── Phase 2: verification (spec §67) ─────────────────────────────────────
    VERIFY_ON_SUBMIT: bool = True
    REDUNDANT_SAMPLE_RATE: float = 0.2         # share of http_check/monitor jobs re-run on a 2nd device
    REDUNDANT_FALLBACK_SCHEMA: bool = True     # no 2nd device available -> schema-only (LOW confidence)
    HTTP_CHECK_LATENCY_TOLERANCE_MS: float = 5000.0

    # ── Phase 2: reputation (spec §36) ───────────────────────────────────────
    REPUTATION_PASS_DELTA: float = 1.0
    REPUTATION_FAIL_DELTA: float = -10.0
    REPUTATION_TIMEOUT_DELTA: float = -3.0
    REPUTATION_CANARY_FAIL_DELTA: float = -25.0
    REPUTATION_QUARANTINE_BELOW: float = 15.0
    VERIFICATION_FAILURES_QUARANTINE: int = 3  # failures within the window -> quarantine
    VERIFICATION_FAILURE_WINDOW_SECONDS: int = 86400


    # ── Phase 3: scheduler (spec §16, §62, §63, §146-148) ────────────────────
    SCHEDULER_INTERVAL_SECONDS: float = 5.0
    SCHEDULER_BATCH_LIMIT: int = 100
    DEVICE_ONLINE_SECONDS: int = 180            # heartbeat freshness for "online"
    MAX_CONCURRENT_JOBS_PER_DEVICE: int = 1
    MAX_INFLIGHT_PER_PROVIDER: int = 500        # per-provider cap
    WORKLOAD_INFLIGHT_CAPS: dict[str, int] = {}  # e.g. {"compression": 50}
    SCHEDULER_AGING_PER_MINUTE: float = 1.0     # anti-starvation priority boost
    SCHEDULER_AGING_MAX: float = 200.0
    JOB_AUTO_SCHEDULE_DEFAULT: bool = True      # new jobs go CREATED->VALIDATED->QUEUED

    # ── Sprint 16: developer platform, prepaid billing, Stripe (spec §101-§104, §134-§136) ──
    # Stripe credentials come ONLY from the environment; nothing is committed.
    # sk_test_/rk_test_ keys -> TEST book. Live keys are refused unless
    # STRIPE_LIVE_MODE_ENABLED=true AND APP_ENV=production (owner go-live switch).
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_API_BASE: str = "https://api.stripe.com"
    STRIPE_LIVE_MODE_ENABLED: bool = False
    STRIPE_WEBHOOK_TOLERANCE_SECONDS: int = 300
    STRIPE_CHECKOUT_SUCCESS_URL: str = "http://localhost:3000/developer?topup=success"
    STRIPE_CHECKOUT_CANCEL_URL: str = "http://localhost:3000/developer?topup=cancel"
    STRIPE_HTTP_TIMEOUT_SECONDS: float = 15.0
    TOPUP_MIN_USD: float = 5.0
    TOPUP_MAX_USD: float = 1000.0
    CUSTOMER_LOW_BALANCE_USD: float = 1.0
    # pricing (spec §170-§172): price = max((fixed costs + verification + risk) * (1+margin)
    # / (1 - variable fee share), minimum); margin floor enforced on every quote
    PRICING_TARGET_MARGIN: float = 0.35
    PRICING_RISK_PREMIUM: float = 0.10
    PRICING_VERIFICATION_USD: float = 0.00002
    PRICING_MIN_PRICE_USD: float = 0.0005
    PRICING_MIN_CONTRIBUTION_MARGIN: float = 0.20
    PRICING_RESERVE_BUFFER: float = 0.20        # maximum_cost = estimate * (1 + buffer); unused part is released
    PRICING_URGENT_PRIORITY: int = 500
    PRICING_URGENCY_PREMIUM: float = 0.25
    QUOTE_TTL_SECONDS: int = 900
    # deployments
    DEPLOYMENT_MIN_INTERVAL_SECONDS: int = 60
    DEPLOYMENT_MAX_RUNS: int = 100000
    DEPLOYMENT_MAX_ACTIVE_PER_CUSTOMER: int = 50
    DEPLOYMENT_MAX_MONITOR_TARGETS: int = 10
    DEPLOYMENT_TICK_BATCH: int = 200
    DEPLOYMENT_RUN_TTL_SECONDS: int = 3600   # an unscheduled run expires (reservation released)
    CUSTOMER_WEBHOOK_MAX_ATTEMPTS: int = 6
    CUSTOMER_WEBHOOK_TIMEOUT_SECONDS: float = 10.0
    CUSTOMER_WEBHOOK_ALLOW_HTTP: bool = False   # dev only: allow http:// in deployment URLs + webhooks
    API_KEY_TOUCH_SECONDS: int = 60             # last_used_at write throttle

    # ── Sprint 10: external adapter hardening (spec §126, §127, §139) ─────────
    ADAPTER_CALL_TIMEOUT_SECONDS: float = 10.0     # every adapter call is bounded
    ADAPTER_CIRCUIT_FAILURE_THRESHOLD: int = 3     # consecutive failures -> circuit opens
    ADAPTER_CIRCUIT_COOLDOWN_SECONDS: int = 300    # open circuit short-circuits calls
    ADAPTER_MAX_OPPORTUNITIES: int = 200           # discovery output cap per adapter

    # ── Phase 3: profitability (spec §17, §64, §65) ──────────────────────────
    # All prices are configuration (no live market feeds, no external accounts).
    POWER_PRICE_KWH_USD: float = 0.15
    DEVICE_IDLE_WATTS: float = 1.5
    DEVICE_ACTIVE_WATTS: float = 4.0            # x (0.5 + thermal_intensity)
    NETWORK_COST_PER_GB_USD: float = 0.01
    INFRA_COST_PER_JOB_USD: float = 0.00001
    INFRA_COST_PER_HOUR_USD: float = 0.001
    PAYOUT_FEE_PCT: float = 0.01                # payment/payout processing on gross
    DEFAULT_DEVICE_PURCHASE_USD: float = 40.0
    DEFAULT_DEVICE_RESALE_USD: float = 8.0
    DEVICE_EXPECTED_LIFETIME_HOURS: float = 8760.0  # active hours over device life
    BASE_FAILURE_PROBABILITY: float = 0.02
    PRICE_SNAPSHOT_USD: dict[str, float] = {"USD": 1.0, "USDC": 1.0, "ACU": 0.02}
    ASSET_VOLATILITY_HAIRCUT: dict[str, float] = {"USD": 0.0, "USDC": 0.0, "ACU": 0.35}
    ASSET_LIQUIDITY_HAIRCUT: dict[str, float] = {"USD": 0.0, "USDC": 0.0, "ACU": 0.20}
    UNKNOWN_ASSET_HAIRCUT: float = 0.5          # never rank unknown tokens at face value

    # ── Phase 4: ledger, revenue confirmation, wallets, withdrawals, payouts ──
    # HARD RULES: no real funds, no mainnet, no private keys. Only the simulated
    # billing source, the mock signer and the simulated payout provider exist.
    LEDGER_TEST_ASSET: str = "TEST_USD"          # TEST book asset (never USDC)
    LEDGER_REAL_ASSET: str = "USDC"              # REAL book asset (no source can post here yet)
    BILLING_SOURCE: str = "simulated"            # only "simulated" is implemented
    REVENUE_SPLIT_DEFAULT: dict[str, float] = {"provider": 0.65, "infrastructure": 0.15, "farmos": 0.20}
    REVENUE_SPLIT_OVERRIDES: dict[str, dict[str, float]] = {}  # key: adapter_id or workload_type
    PROVIDER_EARNINGS_HOLD_HOURS: float = 24.0   # PENDING -> AVAILABLE after confirmation (spec §23/§151)
    OPERATING_RESERVE_TARGET_USD: float = 0.0    # shown in owner accounting (spec §26)
    # Wallet verification (spec §21, §27): EIP-191 personal_sign over an EIP-4361 (SIWE) message.
    WALLET_SIWE_DOMAIN: str = "farmos.local"
    WALLET_SIWE_URI: str = "https://farmos.local"
    WALLET_CHALLENGE_TTL_SECONDS: int = 600
    WALLET_MAX_FAILED_ATTEMPTS: int = 5
    WALLET_CHANGE_COOLDOWN_HOURS: float = 24.0
    WALLET_NETWORKS: dict[str, int] = {"base": 8453, "base-sepolia": 84532}  # ownership proof only
    # Withdrawals (spec §74, §106, §149)
    WITHDRAWAL_MIN_USD: float = 5.0
    WITHDRAWAL_MAX_PER_TX_USD: float = 1000.0
    WITHDRAWAL_DAILY_LIMIT_USD: float = 2000.0
    WITHDRAWAL_NEW_ACCOUNT_DAYS: int = 7
    WITHDRAWAL_NEW_ACCOUNT_DAILY_LIMIT_USD: float = 100.0
    WITHDRAWAL_STANDARD_HOLD_HOURS: float = 1.0
    WITHDRAWAL_EXTENDED_HOLD_HOURS: float = 72.0
    WITHDRAWAL_LARGE_USD: float = 500.0          # needs two distinct approvers
    WITHDRAWAL_ALLOW_SELF_APPROVAL: bool = False
    WITHDRAWAL_REAL_BOOK_ENABLED: bool = False   # REAL USDC withdrawals are refused in this build
    # Payouts (spec §107-§111): simulated provider + mock signer only.
    PAYOUT_PROVIDER: str = "simulated"
    PAYOUT_SIGNER: str = "mock"
    PAYOUT_NETWORKS: dict[str, int] = {"base-sepolia": 84532}  # testnet label; mainnet is hard-blocked
    PAYOUT_CONFIRMATIONS_REQUIRED: int = 3
    PAYOUT_BATCH_SIZE: int = 50
    PAYOUT_DAILY_LIMIT_USD: float = 5000.0
    PAYOUT_MAX_TX_USD: float = 1000.0
    PAYOUT_HOT_WALLET_ADDRESS: str = "0x000000000000000000000000000000000000F4A1"  # simulated only
    TOKEN_REGISTRY: dict[str, dict] = {
        "USDC": {"base-sepolia": {"chain_id": 84532, "decimals": 6, "status": "ACTIVE",
                                  "contract": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                                  "source_of_truth": "Circle testnet docs; re-verify before any non-simulated use"}},
    }
    RECONCILIATION_TOLERANCE_USD: float = 0.000001


settings = Settings()