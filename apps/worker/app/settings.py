"""ARQ settings for the private worker process.

Run with: ``arq app.settings.WorkerSettings``. REDIS_URL must be supplied by the
orchestrator; no connection is opened until the ARQ process starts.
"""

from typing import Any

from arq.connections import RedisSettings

from app.config import WorkerSettings as EnvWorkerSettings
from app.jobs import (
    expire_reservations,
    process_webhook,
    reconciliation,
    send_email,
    settle_credits,
    usage_rollup,
)
from app.logging import configure_logging

_config = EnvWorkerSettings()  # type: ignore[call-arg]  # REDIS_URL is read from env.


def _redis_settings() -> RedisSettings:
    """Build ARQ's Redis connection settings from the required private DSN."""
    return RedisSettings.from_dsn(str(_config.redis_url))


async def _on_startup(ctx: dict[str, Any]) -> None:
    """Apply the worker's redacting JSON logging setup at process startup."""
    del ctx
    configure_logging(_config.log_level)


class WorkerSettings:
    """ARQ-discoverable settings object for the Phase 1 worker skeleton."""

    redis_settings = _redis_settings()
    functions = [
        settle_credits,
        process_webhook,
        usage_rollup,
        reconciliation,
        expire_reservations,
        send_email,
    ]
    queue_name = _config.queue_name
    max_jobs = _config.max_jobs
    job_timeout = _config.job_timeout_seconds
    keep_result = _config.result_ttl_seconds
    on_startup = _on_startup
