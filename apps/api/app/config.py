"""Application configuration.

All values come from environment variables. Secrets intentionally have no
defaults so a misconfigured deployment fails fast instead of silently running
with a well-known value.
"""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the public FastAPI gateway."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Deployment
    environment: str = "development"
    log_level: str = "INFO"
    app_url: str = "http://localhost:3000"
    api_base_url: str = "http://localhost:8000"

    # Private dependencies (never exposed to clients)
    database_url: str
    redis_url: str
    litellm_base_url: str = "http://litellm:4000"
    litellm_master_key: str

    # Dashboard auth
    jwt_secret: str
    jwt_issuer: str = "ai-api-gateway"
    jwt_audience: str = "ai-api-gateway-dashboard"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = Field(default=60, ge=5, le=1440)

    # API key hashing
    api_key_pepper: str

    # Upstream HTTP client timeouts (seconds)
    http_connect_timeout: float = Field(default=5.0, gt=0)
    http_read_timeout: float = Field(default=300.0, gt=0)

    # Reserved for Phase 2 billing alerts; signed microcredits (BIGINT).
    spend_alert_threshold_microcredits: int = Field(default=0, ge=0)

    # Phase 4 /v1 gateway limits. All env-only; no secret defaults.
    # Upper bound on the caller-requested ``max_tokens`` (output budget).
    max_output_tokens: int = Field(default=4096, ge=1, le=128000)
    # Largest accepted request body in bytes (1 MiB by default).
    max_request_bytes: int = Field(default=1048576, ge=1024)
    # Per-API-key sliding-window request budget per minute.
    rate_limit_requests_per_minute: int = Field(default=60, ge=1)
    # Per-API-key cap on concurrently in-flight chat completions.
    rate_limit_max_concurrent: int = Field(default=8, ge=1)
    # Credit reservation hold lifetime before it is eligible for expiry release.
    reservation_ttl_seconds: int = Field(default=900, ge=1, le=86400)

    # Phase 5 / Stripe. All credentials and price ids are env-only and blank by
    # default so local deployments boot without any Stripe configuration.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_10: str = ""
    stripe_price_50: str = ""
    stripe_price_100: str = ""
    stripe_success_url: str = "http://localhost:3000/dashboard/billing?checkout=success"
    stripe_cancel_url: str = "http://localhost:3000/dashboard/billing?checkout=cancelled"

    @field_validator(
        "database_url", "redis_url", "litellm_master_key", "jwt_secret", "api_key_pepper"
    )
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("litellm_base_url", "app_url", "api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        normalised = value.strip().upper()
        if normalised not in allowed:
            raise ValueError(f"must be one of {', '.join(sorted(allowed))}")
        return normalised

    @field_validator("jwt_algorithm")
    @classmethod
    def _pin_jwt_algorithm(cls, value: str) -> str:
        # Verification always pins HS256; refuse a contradictory configuration.
        if value.strip().upper() != "HS256":
            raise ValueError("only HS256 is supported")
        return "HS256"

    @property
    def cors_origins(self) -> list[str]:
        """Dashboard origin derived from ``app_url`` (scheme + host + port).

        The public ``/v1`` API is intentionally not given CORS access: browser
        clients should only call the dashboard origin, which proxies privately.
        """
        parts = urlsplit(self.app_url)
        if not parts.scheme or not parts.netloc:
            raise ValueError("app_url must be an absolute URL")
        return [f"{parts.scheme}://{parts.netloc}"]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def stripe_enabled(self) -> bool:
        """True when a Stripe secret key has been supplied via the environment."""
        return bool(self.stripe_secret_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
