"""Job queue abstraction tests (arq mocked, no Redis)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.services.job_queue import ArqJobQueue, get_job_queue


class FakePool:
    """Minimal arq pool double that records enqueues and closure."""

    def __init__(self, *, fail_enqueue: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.fail_enqueue = fail_enqueue

    async def enqueue_job(self, function: str, **kwargs: Any) -> None:
        if self.fail_enqueue:
            raise RuntimeError("redis down")
        self.calls.append({"function": function, "kwargs": kwargs})

    async def aclose(self) -> None:
        self.closed = True


def _patch_create_pool(monkeypatch: pytest.MonkeyPatch, pool: FakePool) -> list[Any]:
    dsn_seen: list[Any] = []

    async def _fake_create_pool(settings: Any, **kwargs: Any) -> FakePool:
        dsn_seen.append(settings)
        return pool

    monkeypatch.setattr("arq.connections.create_pool", _fake_create_pool)
    return dsn_seen


async def test_arq_job_queue_enqueues_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    dsn_seen = _patch_create_pool(monkeypatch, pool)

    queue = ArqJobQueue("redis://127.0.0.1:6379/0")
    await queue.enqueue_job("process_webhook", event_id="uuid-123")

    assert pool.calls == [{"function": "process_webhook", "kwargs": {"event_id": "uuid-123"}}]
    assert pool.closed is True
    assert len(dsn_seen) == 1
    assert dsn_seen[0].host == "127.0.0.1"


async def test_arq_job_queue_closes_pool_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(fail_enqueue=True)
    _patch_create_pool(monkeypatch, pool)

    queue = ArqJobQueue("redis://127.0.0.1:6379/0")
    with pytest.raises(RuntimeError, match="redis down"):
        await queue.enqueue_job("process_webhook", event_id="uuid-123")
    assert pool.closed is True


def test_get_job_queue_uses_app_settings(settings: Settings) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    queue = get_job_queue(request)  # type: ignore[arg-type]
    assert isinstance(queue, ArqJobQueue)
