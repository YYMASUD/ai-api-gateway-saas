"""Per-API-key sliding-window request rate and in-flight concurrency limits.

Both checks are Redis-backed and **fail OPEN**: a Redis outage must never
block legitimate traffic, so any Redis exception is logged as a redacted
warning (event name + error class only, never key material or payloads) and
the request is allowed through. This is a deliberate availability-over-
strictness trade-off documented here so it cannot be mistaken for a bug.

Keys
----
``rl:req:{api_key_id}`` — sorted set of request timestamps scored by epoch
    milliseconds. Members older than the 60s window are pruned on every
    check, so the set stays bounded by the per-minute budget.
``rl:conc:{api_key_id}`` — integer counter of in-flight requests: INCR on
    acquire, DECR on release. A TTL guards against leaked slots if a worker
    dies mid-request.

Retry-After
-----------
Both limiters raise ``RateLimitError`` carrying a whole-second
``retry_after``; the route copies it onto the ``Retry-After`` header.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.errors import RateLimitError
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["RateLimiter", "concurrency_slot"]

_WINDOW_SECONDS = 60
_REQUEST_KEY_TTL_SECONDS = 120
_CONCURRENCY_KEY_TTL_SECONDS = 3600


def request_key(api_key_id: str) -> str:
    return f"rl:req:{api_key_id}"


def concurrency_key(api_key_id: str) -> str:
    return f"rl:conc:{api_key_id}"


class RateLimiter:
    """Sliding-window request budget plus an in-flight concurrency cap."""

    def __init__(
        self,
        redis: Any,
        *,
        requests_per_minute: int,
        max_concurrent: int,
        window_seconds: int = _WINDOW_SECONDS,
    ) -> None:
        self._redis = redis
        self._rpm = requests_per_minute
        self._max_concurrent = max_concurrent
        self._window_ms = window_seconds * 1000

    async def check_request_rate(self, api_key_id: str) -> None:
        """Raise ``RateLimitError`` when the per-minute budget is exhausted.

        Fail-open on any Redis error: the warning is redacted and the call
        returns normally.
        """
        if self._redis is None:
            return
        now_ms = int(time.time() * 1000)
        key = request_key(api_key_id)
        member = f"{now_ms}:{time.monotonic_ns()}"
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.zremrangebyscore(key, "-inf", now_ms - self._window_ms)
            pipe.zadd(key, {member: now_ms})
            pipe.zcard(key)
            pipe.expire(key, _REQUEST_KEY_TTL_SECONDS)
            results = await pipe.execute()
            count = int(results[2] or 0)
        except Exception as exc:
            logger.warning("rate_limit_request_check_failed", error_type=type(exc).__name__)
            return
        if count <= self._rpm:
            return
        # Over budget: drop our own member so denied attempts do not extend
        # the window, then compute how long until the oldest entry ages out.
        retry_after = 1
        try:
            await self._redis.zrem(key, member)
            oldest = await self._redis.zrange(key, 0, 0, withscores=True)
            if oldest:
                _, score = oldest[0]
                remaining_ms = (int(score) + self._window_ms) - now_ms
                retry_after = max(1, math.ceil(remaining_ms / 1000))
        except Exception as exc:
            logger.warning("rate_limit_retry_after_failed", error_type=type(exc).__name__)
        raise RateLimitError(
            "Request rate limit exceeded", code="rate_limit_exceeded", retry_after=retry_after
        )

    async def acquire_concurrency(self, api_key_id: str) -> bool:
        """Take an in-flight slot; ``False`` when the cap is reached.

        Fail-open on Redis errors (returns ``True``). When the cap is hit the
        counter is decremented again immediately, so a denied caller never
        owns a slot and must NOT release one.
        """
        if self._redis is None:
            return True
        key = concurrency_key(api_key_id)
        try:
            current = int(await self._redis.incr(key))
            await self._redis.expire(key, _CONCURRENCY_KEY_TTL_SECONDS)
        except Exception as exc:
            logger.warning("rate_limit_concurrency_acquire_failed", error_type=type(exc).__name__)
            return True
        if current > self._max_concurrent:
            try:
                await self._redis.decr(key)
            except Exception as exc:
                logger.warning(
                    "rate_limit_concurrency_rollback_failed", error_type=type(exc).__name__
                )
            return False
        return True

    async def release_concurrency(self, api_key_id: str) -> None:
        """Give back an in-flight slot; never raises."""
        if self._redis is None:
            return
        key = concurrency_key(api_key_id)
        try:
            value = int(await self._redis.decr(key))
            if value < 0:
                # Defensive: a leaked/duplicated release must not leave a
                # negative counter that would grant extra capacity later.
                await self._redis.delete(key)
        except Exception as exc:
            logger.warning("rate_limit_concurrency_release_failed", error_type=type(exc).__name__)


@asynccontextmanager
async def concurrency_slot(limiter: RateLimiter, api_key_id: str) -> AsyncIterator[None]:
    """Hold an in-flight slot for the duration of the block.

    Raises ``RateLimitError`` (429, ``retry_after=1``) when the cap is
    reached. The slot is released in ``finally`` so normal completion,
    exceptions, streaming bodies and client disconnects all unwind cleanly.
    """
    if not await limiter.acquire_concurrency(api_key_id):
        raise RateLimitError(
            "Too many concurrent requests", code="rate_limit_exceeded", retry_after=1
        )
    try:
        yield
    finally:
        await limiter.release_concurrency(api_key_id)
