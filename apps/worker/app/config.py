"""Environment-only configuration for the private background worker.

Deployment assumptions:
    * The worker runs on a private network and exposes no public ports.
    * Redis is reached through internal service DNS or a private VPC address.
    * The orchestrator injects REDIS_URL from a secret manager at runtime.
    * Source code, images, and tests contain no credentials or secret defaults.
"""

from typing import Literal

from pydantic import Field, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Worker settings sourced from environment variables only.

    REDIS_URL is required and must point at the private Redis endpoint. Non-secret
    operational defaults below may be overridden with WORKER_-prefixed variables.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="WORKER_",
        case_sensitive=False,
        extra="ignore",
    )

    redis_url: RedisDsn = Field(
        ...,
        alias="REDIS_URL",
        description="Private Redis DSN injected by the deployment secret manager.",
    )
    queue_name: str = Field(default="arq:queue", min_length=1)
    max_jobs: int = Field(default=16, ge=1)
    job_timeout_seconds: int = Field(default=300, ge=1)
    result_ttl_seconds: int = Field(default=3600, ge=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
