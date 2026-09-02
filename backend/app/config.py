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


settings = Settings()