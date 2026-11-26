"""Outbound job queue abstraction for the public API.

The API enqueues work by job *name*; it never imports worker code. The arq
connection is created lazily per enqueue and closed immediately, so a webhook
request does not hold a Redis pool open. Tests override ``get_job_queue`` with
an in-memory fake that records enqueues.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import Request

from app.config import Settings, get_settings

__all__ = ["ArqJobQueue", "JobQueue", "get_job_queue"]

PROCESS_WEBHOOK_JOB = "process_webhook"


class JobQueue(Protocol):
    """Persistence boundary for background jobs (overridable in tests)."""

    async def enqueue_job(self, function: str, **kwargs: Any) -> None:
        """Enqueue ``function`` with keyword arguments; no result is required."""
        ...


class ArqJobQueue:
    """arq-backed implementation using the shared private Redis URL."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url

    async def enqueue_job(self, function: str, **kwargs: Any) -> None:
        from arq.connections import RedisSettings, create_pool

        pool = await create_pool(RedisSettings.from_dsn(self._redis_url))
        try:
            # The worker registers ``process_webhook``; pass only the opaque
            # webhook-event id, never customer or card data.
            await pool.enqueue_job(function, **kwargs)
        finally:
            await pool.aclose()


def get_job_queue(request: Request) -> JobQueue:
    """FastAPI dependency returning a queue bound to the configured Redis URL."""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    resolved = settings if settings is not None else get_settings()
    return ArqJobQueue(resolved.redis_url)
