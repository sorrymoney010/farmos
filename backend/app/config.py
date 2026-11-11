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


settings = Settings()