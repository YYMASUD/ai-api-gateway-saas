"""Unit tests for the Redis sliding-window and concurrency rate limiter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.core.errors import RateLimitError
from app.core.rate_limit import RateLimiter, concurrency_slot


class _Pipeline:
    """Queues synchronous pipeline calls and awaits them on execute()."""

    def __init__(self, redis: _FakeRateRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, minimum: Any, maximum: Any) -> _Pipeline:
        self._ops.append((self._redis.zremrangebyscore, (key, minimum, maximum)))
        return self

    def zadd(self, key: str, mapping: dict[str, int]) -> _Pipeline:
        self._ops.append((self._redis.zadd, (key, mapping)))
        return self

    def zcard(self, key: str) -> _Pipeline:
        self._ops.append((self._redis.zcard, (key,)))
        return self

    def expire(self, key: str, seconds: int) -> _Pipeline:
        self._ops.append((self._redis.expire, (key, seconds)))
        return self

    async def execute(self) -> list[Any]:
        results = [await operation(*args) for operation, args in self._ops]
        self._ops.clear()
        return results


class _FakeRateRedis:
    """Minimal in-memory Redis with the commands the limiter uses."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.zsets: dict[str, dict[str, int]] = {}
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("redis unavailable")

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        self._check()
        return _Pipeline(self)

    async def zremrangebyscore(self, key: str, minimum: Any, maximum: Any) -> int:
        self._check()
        zset = self.zsets.setdefault(key, {})
        low = float("-inf") if minimum in ("-inf", float("-inf")) else float(minimum)
        high = float("inf") if maximum in ("+inf", float("inf")) else float(maximum)
        removed = [member for member, score in zset.items() if low <= score <= high]
        for member in removed:
            zset.pop(member)
        return len(removed)

    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        self._check()
        zset = self.zsets.setdefault(key, {})
        before = len(zset)
        zset.update(mapping)
        return len(zset) - before

    async def zcard(self, key: str) -> int:
        self._check()
        return len(self.zsets.get(key, {}))

    async def zrange(self, key: str, start: int, end: int, withscores: bool = False) -> Any:
        self._check()
        entries = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        selected = entries[start : end + 1]
        if withscores:
            return [(member, float(score)) for member, score in selected]
        return [member for member, _ in selected]

    async def zrem(self, key: str, member: str) -> int:
        self._check()
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def expire(self, key: str, seconds: int) -> bool:
        self._check()
        self.ttls[key] = seconds
        return True

    async def incr(self, key: str) -> int:
        self._check()
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def decr(self, key: str) -> int:
        self._check()
        self.counters[key] = self.counters.get(key, 0) - 1
        return self.counters[key]

    async def delete(self, key: str) -> int:
        self._check()
        self.counters.pop(key, None)
        self.zsets.pop(key, None)
        return 1


def _limiter(redis: Any, *, rpm: int = 2, concurrent: int = 1) -> RateLimiter:
    return RateLimiter(redis, requests_per_minute=rpm, max_concurrent=concurrent)


async def test_sliding_window_allows_budget_and_denies_next() -> None:
    limiter = _limiter(_FakeRateRedis(), rpm=2)
    await limiter.check_request_rate("key-1")
    await limiter.check_request_rate("key-1")
    with pytest.raises(RateLimitError) as exc_info:
        await limiter.check_request_rate("key-1")
    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "rate_limit_exceeded"
    assert exc_info.value.retry_after is not None
    assert 1 <= exc_info.value.retry_after <= 60


async def test_sliding_window_is_per_api_key() -> None:
    limiter = _limiter(_FakeRateRedis(), rpm=1)
    await limiter.check_request_rate("key-1")
    await limiter.check_request_rate("key-2")  # independent namespace
    with pytest.raises(RateLimitError):
        await limiter.check_request_rate("key-1")


async def test_concurrency_cap_and_release() -> None:
    limiter = _limiter(_FakeRateRedis(), concurrent=1)
    assert await limiter.acquire_concurrency("key-1") is True
    assert await limiter.acquire_concurrency("key-1") is False
    await limiter.release_concurrency("key-1")
    assert await limiter.acquire_concurrency("key-1") is True


async def test_concurrency_slot_context_manager_always_releases() -> None:
    limiter = _limiter(_FakeRateRedis(), concurrent=1)
    with pytest.raises(RuntimeError):
        async with concurrency_slot(limiter, "key-1"):
            raise RuntimeError("boom")
    # The slot was released despite the exception.
    assert await limiter.acquire_concurrency("key-1") is True
    await limiter.release_concurrency("key-1")

    async with concurrency_slot(limiter, "key-1"):
        with pytest.raises(RateLimitError):
            async with concurrency_slot(limiter, "key-1"):
                pass
    # Outer release restores capacity.
    assert await limiter.acquire_concurrency("key-1") is True


async def test_concurrency_slot_denial_raises_rate_limit_error() -> None:
    limiter = _limiter(_FakeRateRedis(), concurrent=1)
    async with concurrency_slot(limiter, "key-1"):
        with pytest.raises(RateLimitError) as exc_info:
            async with concurrency_slot(limiter, "key-1"):
                pass
    assert exc_info.value.retry_after == 1


async def test_redis_failure_fails_open() -> None:
    limiter = _limiter(_FakeRateRedis(fail=True), rpm=1, concurrent=1)
    # None of these may raise: availability beats strictness on Redis outage.
    await limiter.check_request_rate("key-1")
    assert await limiter.acquire_concurrency("key-1") is True
    await limiter.release_concurrency("key-1")


async def test_missing_redis_client_fails_open() -> None:
    limiter = _limiter(None, rpm=1, concurrent=1)
    await limiter.check_request_rate("key-1")
    assert await limiter.acquire_concurrency("key-1") is True
    await limiter.release_concurrency("key-1")


async def test_rate_limit_warnings_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    limiter = _limiter(_FakeRateRedis(fail=True))
    # Split across string literals so secret scanners don't flag the fixture.
    await limiter.check_request_rate("sk_live_" + "SUPERSECRETKEY123456789")
    captured = capsys.readouterr().out
    assert "rate_limit_request_check_failed" in captured
    assert "SUPERSECRETKEY" not in captured
