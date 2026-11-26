"""ARQ settings for the private worker process.

Run with: ``arq worker_app.settings.WorkerSettings``. REDIS_URL and DATABASE_URL
must be supplied by the orchestrator at runtime. Importing this module performs
no environment reads and opens no connections: the env-backed attributes below
resolve on first access, so ``worker_app.settings`` can be imported (by tests,
tooling, or the API co-install) before deployment secrets exist.
"""

from collections.abc import Callable
from typing import Any

from arq.connections import RedisSettings

from worker_app.config import WorkerSettings as EnvWorkerSettings
from worker_app.jobs import (
    expire_reservations,
    process_webhook,
    reconciliation,
    send_email,
    settle_credits,
    usage_rollup,
)
from worker_app.logging import configure_logging

_config_cache: EnvWorkerSettings | None = None


def _config() -> EnvWorkerSettings:
    """Read the required environment exactly once, on first use."""
    global _config_cache
    if _config_cache is None:
        _config_cache = EnvWorkerSettings()  # type: ignore[call-arg]  # read from env.
    return _config_cache


def _redis_settings() -> RedisSettings:
    """Build ARQ's Redis connection settings from the required private DSN."""
    return RedisSettings.from_dsn(str(_config().redis_url))


# Env-backed attributes resolved lazily. ARQ's ``get_kwargs`` copies
# ``WorkerSettings.__dict__`` verbatim and does NOT honour descriptors, so the
# metaclass below must write real values into the class dict before that read.
_LAZY_SETTINGS: dict[str, Callable[[], Any]] = {
    "redis_settings": _redis_settings,
    "queue_name": lambda: _config().queue_name,
    "max_jobs": lambda: _config().max_jobs,
    "job_timeout": lambda: _config().job_timeout_seconds,
    "keep_result": lambda: _config().result_ttl_seconds,
}


class _LazySettingsMeta(type):
    """Materialize env-backed settings on first access instead of at import."""

    def __getattribute__(cls, name: str) -> Any:
        namespace = type.__getattribute__(cls, "__dict__")
        if name == "__dict__":
            # ARQ reads the raw class dict; resolve every pending setting so
            # it receives real values, never loader callables.
            for key, loader in _LAZY_SETTINGS.items():
                if key not in namespace:
                    type.__setattr__(cls, key, loader())
        elif name in _LAZY_SETTINGS and name not in namespace:
            type.__setattr__(cls, name, _LAZY_SETTINGS[name]())
        return type.__getattribute__(cls, name)


async def _on_startup(ctx: dict[str, Any]) -> None:
    """Apply the worker's redacting JSON logging setup at process startup."""
    del ctx
    configure_logging(_config().log_level)


class WorkerSettings(metaclass=_LazySettingsMeta):
    """ARQ-discoverable settings object for the worker process."""

    # Annotations only: values are injected by _LazySettingsMeta on first use.
    redis_settings: RedisSettings
    queue_name: str
    max_jobs: int
    job_timeout: int
    keep_result: int

    functions = [
        settle_credits,
        process_webhook,
        usage_rollup,
        reconciliation,
        expire_reservations,
        send_email,
    ]
    on_startup = _on_startup
