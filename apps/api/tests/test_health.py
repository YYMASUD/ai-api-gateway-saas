"""Tests for liveness and readiness endpoints with mocked dependencies."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.routers import health

SENSITIVE = "SENSITIVE-MUST-NOT-APPEAR"


class _OkConnection:
    async def execute(self, statement: Any) -> None:
        return None

    async def __aenter__(self) -> _OkConnection:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeEngine:
    """Minimal async engine stand-in for ``check_database``."""

    def connect(self) -> _OkConnection:
        return _OkConnection()


class _BrokenConnection:
    async def execute(self, statement: Any) -> None:
        raise RuntimeError(f"DB_PASSWORD={SENSITIVE}")

    async def __aenter__(self) -> _BrokenConnection:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _BrokenEngine:
    def connect(self) -> _BrokenConnection:
        return _BrokenConnection()


class _RaisingConnectEngine:
    """Engine whose ``connect`` raises before any connection is returned."""

    def connect(self) -> Any:
        raise RuntimeError(f"DB_PASSWORD={SENSITIVE}")


class _FakeRedis:
    def __init__(self, *, ping_ok: bool = True) -> None:
        self.ping_ok = ping_ok
        self.closed = False
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        return self.ping_ok

    async def aclose(self) -> None:
        self.closed = True


async def _ok_database(*_args: Any, **_kwargs: Any) -> health.DependencyCheck:
    return health.DependencyCheck("database", ok=True, latency_ms=1.0)


async def _ok_redis(*_args: Any, **_kwargs: Any) -> health.DependencyCheck:
    return health.DependencyCheck("redis", ok=True, latency_ms=1.0)


async def _ok_litellm(*_args: Any, **_kwargs: Any) -> health.DependencyCheck:
    return health.DependencyCheck("litellm", ok=True, latency_ms=1.0)


async def _failed_redis(*_args: Any, **_kwargs: Any) -> health.DependencyCheck:
    return health.DependencyCheck("redis", ok=False, error="connection failed")


async def _failed_litellm(*_args: Any, **_kwargs: Any) -> health.DependencyCheck:
    return health.DependencyCheck("litellm", ok=False, error="connection failed")


_FAILING = {"check_redis": _failed_redis, "check_litellm_readiness": _failed_litellm}


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _client(app: FastAPI, settings: Settings) -> TestClient:
    # The readiness probe must not depend on get_db; only settings is injected.
    app.dependency_overrides[health.get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


def test_healthz_liveness_requires_no_dependencies(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize("check_name", ["check_redis", "check_litellm_readiness"])
def test_readyz_success_with_mocked_dependencies(
    app: FastAPI,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    check_name: str,
) -> None:
    monkeypatch.setattr(health, "check_redis", _ok_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", _ok_litellm)
    monkeypatch.setattr(health, "check_database", _ok_database)
    with _client(app, settings) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "redis", "litellm"}
    assert all(check["status"] == "ok" for check in body["checks"].values())


@pytest.mark.parametrize("check_name", ["check_redis", "check_litellm_readiness"])
def test_readyz_returns_503_without_leaking_secrets(
    app: FastAPI,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    check_name: str,
) -> None:
    monkeypatch.setattr(health, "check_redis", _ok_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", _ok_litellm)
    monkeypatch.setattr(health, "check_database", _ok_database)
    monkeypatch.setattr(health, check_name, _FAILING[check_name])
    expected_failing = "redis" if check_name == "check_redis" else "litellm"
    with _client(app, settings) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert SENSITIVE not in response.text
    assert body["checks"][expected_failing]["status"] == "unavailable"
    assert body["checks"]["database"]["status"] == "ok"


def test_readyz_database_failure_is_503_envelope(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "check_redis", _ok_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", _ok_litellm)
    monkeypatch.setattr(health, "check_database", health.check_database)
    monkeypatch.setattr(health.database_state, "engine", _BrokenEngine())
    monkeypatch.setattr(health.database_state, "dispose", _async_noop)
    with _client(app, settings) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["status"] == "unavailable"
    assert SENSITIVE not in response.text


def test_readyz_uninitialised_database_is_503_not_500(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prevent the lifespan from building a real engine, leaving the DB
    # uninitialised exactly as it is when the DB has never come up.
    monkeypatch.setattr(health.database_state, "init", _async_noop)
    monkeypatch.setattr(health.database_state, "engine", None)
    monkeypatch.setattr(health, "check_redis", _ok_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", _ok_litellm)
    with _client(app, settings) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["database"]["status"] == "unavailable"
    assert set(body["checks"]) == {"database", "redis", "litellm"}


def test_readyz_reuses_application_shared_clients(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def record_redis(_settings: Settings, client: Any = None) -> health.DependencyCheck:
        seen["redis"] = client
        return health.DependencyCheck("redis", ok=True, latency_ms=1.0)

    async def record_litellm(_settings: Settings, client: Any = None) -> health.DependencyCheck:
        seen["litellm"] = client
        return health.DependencyCheck("litellm", ok=True, latency_ms=1.0)

    monkeypatch.setattr(health, "check_database", _ok_database)
    monkeypatch.setattr(health, "check_redis", record_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", record_litellm)
    with _client(app, settings) as client:
        response = client.get("/readyz")
        assert seen["redis"] is app.state.redis
        assert seen["litellm"] is app.state.litellm_client
    assert response.status_code == 200


async def test_database_check_reports_unavailable_when_engine_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.database_state, "engine", None)
    result = await health.check_database()
    assert not result.ok
    assert result.error == "not initialised"


async def test_database_check_success_and_failure() -> None:
    ok = await health.check_database(_FakeEngine())  # type: ignore[arg-type]
    assert ok.ok
    assert ok.name == "database"
    broken = await health.check_database(_BrokenEngine())  # type: ignore[arg-type]
    assert not broken.ok
    assert broken.error == "connection failed"
    refused = await health.check_database(_RaisingConnectEngine())  # type: ignore[arg-type]
    assert not refused.ok
    assert refused.error == "connection failed"


async def test_check_redis_does_not_close_injected_client(settings: Settings) -> None:
    client = _FakeRedis()
    result = await health.check_redis(settings, client)  # type: ignore[arg-type]
    assert result.ok
    assert client.pings == 1
    assert client.closed is False


async def test_check_redis_closes_fallback_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeRedis()
    monkeypatch.setattr(health, "create_redis_client", lambda _settings: client)
    result = await health.check_redis(settings)
    assert result.ok
    assert client.closed is True


async def test_check_redis_reports_ping_failure_without_closing_injected_client(
    settings: Settings,
) -> None:
    client = _FakeRedis(ping_ok=False)
    result = await health.check_redis(settings, client)  # type: ignore[arg-type]
    assert not result.ok
    assert result.error == "ping failed"
    assert client.closed is False


async def test_check_redis_reports_unavailable_when_client_creation_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_settings: Settings) -> Any:
        raise RuntimeError(f"REDIS_URL={SENSITIVE}")

    monkeypatch.setattr(health, "create_redis_client", boom)
    result = await health.check_redis(settings)
    assert not result.ok
    assert result.error == "connection failed"
    assert SENSITIVE not in (result.error or "")


async def test_check_redis_swallows_fallback_close_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CloseFailingRedis(_FakeRedis):
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(health, "create_redis_client", lambda _settings: CloseFailingRedis())
    result = await health.check_redis(settings)
    assert result.ok


async def test_litellm_readiness_check_uses_private_base_url(settings: Settings) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await health.check_litellm_readiness(settings, client)
    assert result.ok
    assert seen["url"] == f"{settings.litellm_base_url}/health/readiness"
    assert seen["authorization"] == f"Bearer {settings.litellm_master_key}"


async def test_litellm_readiness_check_reports_non_200(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await health.check_litellm_readiness(settings, client)
    assert not result.ok
    assert result.error == "status 503"


async def test_litellm_readiness_check_reports_connection_failure(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await health.check_litellm_readiness(settings, client)
    assert not result.ok
    assert result.error == "connection failed"


async def test_litellm_readiness_check_survives_client_construction_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("client construction failed")

    monkeypatch.setattr(health.httpx, "AsyncClient", boom)
    result = await health.check_litellm_readiness(settings)
    assert not result.ok
    assert result.name == "litellm"
    assert result.error == "connection failed"


async def test_litellm_fallback_client_is_closed_after_success(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[httpx.AsyncClient] = []
    real_client_cls = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(health.httpx, "AsyncClient", factory)
    result = await health.check_litellm_readiness(settings)
    assert result.ok
    assert len(created) == 1
    assert created[0].is_closed


async def test_litellm_swallows_fallback_close_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubResponse:
        status_code = 200

    class CloseFailingClient:
        async def get(self, *_args: Any, **_kwargs: Any) -> StubResponse:
            return StubResponse()

        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(health.httpx, "AsyncClient", lambda **_kwargs: CloseFailingClient())
    result = await health.check_litellm_readiness(settings)
    assert result.ok
