"""Tests for liveness and readiness endpoints with mocked dependencies."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import get_db
from app.routers import health


class FakeSession:
    async def execute(self, statement: Any) -> None:
        return None


class BrokenSession:
    async def execute(self, statement: Any) -> None:
        raise RuntimeError("DB_PASSWORD=SENSITIVE-MUST-NOT-APPEAR")


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


def _client(app: FastAPI, session: Any) -> TestClient:
    async def fake_db() -> Any:
        yield session

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_settings] = get_settings
    return TestClient(app, raise_server_exceptions=False)


def test_healthz_liveness_requires_no_dependencies(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize("check_name", ["check_redis", "check_litellm_readiness"])
def test_readyz_success_with_mocked_dependencies(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, check_name: str
) -> None:
    monkeypatch.setattr(health, "check_redis", _ok_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", _ok_litellm)
    with _client(app, FakeSession()) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "redis", "litellm"}
    assert all(check["status"] == "ok" for check in body["checks"].values())


@pytest.mark.parametrize("check_name", ["check_redis", "check_litellm_readiness"])
def test_readyz_returns_503_without_leaking_secrets(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, check_name: str
) -> None:
    monkeypatch.setattr(health, "check_redis", _ok_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", _ok_litellm)
    monkeypatch.setattr(health, check_name, _FAILING[check_name])
    expected_failing = "redis" if check_name == "check_redis" else "litellm"
    with _client(app, FakeSession()) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "SENSITIVE-MUST-NOT-APPEAR" not in response.text
    assert body["checks"][expected_failing]["status"] == "unavailable"
    assert body["checks"]["database"]["status"] == "ok"


def test_readyz_fails_when_database_unavailable(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "check_redis", _ok_redis)
    monkeypatch.setattr(health, "check_litellm_readiness", _ok_litellm)
    with _client(app, BrokenSession()) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["status"] == "unavailable"
    assert "SENSITIVE-MUST-NOT-APPEAR" not in response.text


async def test_database_check_success_and_failure() -> None:
    ok = await health.check_database(FakeSession())  # type: ignore[arg-type]
    assert ok.ok
    assert ok.name == "database"
    broken = await health.check_database(BrokenSession())  # type: ignore[arg-type]
    assert not broken.ok
    assert broken.error == "connection failed"


async def test_litellm_readiness_check_uses_private_base_url(settings) -> None:
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


async def test_litellm_readiness_check_reports_non_200(settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await health.check_litellm_readiness(settings, client)
    assert not result.ok
    assert result.error == "status 503"


async def test_litellm_readiness_check_reports_connection_failure(settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await health.check_litellm_readiness(settings, client)
    assert not result.ok
    assert result.error == "connection failed"
