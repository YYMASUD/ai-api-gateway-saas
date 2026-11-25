"""Shared async Redis client factory.

Phase 1 only needs connectivity for readiness checks and future rate limiting.
The client is created lazily and closed on application shutdown.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    """Return an async Redis client configured from ``REDIS_URL``."""
    return Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_connect_timeout=settings.http_connect_timeout,
        socket_timeout=settings.http_read_timeout,
        health_check_interval=30,
    )


async def ping_redis(client: Redis) -> bool:
    """Return ``True`` when Redis answers PING."""
    try:
        return bool(await client.ping())
    except Exception:
        return False
