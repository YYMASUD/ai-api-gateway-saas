"""Tests for the FastAPI application factory and shared resource lifespan."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.session import database_state
from app.main import create_app

API_ROOT = Path(__file__).resolve().parents[1]

_REQUIRED_ENV = {
    "DATABASE_URL",
    "REDIS_URL",
    "LITELLM_MASTER_KEY",
    "JWT_SECRET",
    "API_KEY_PEPPER",
}


def test_importing_app_main_does_not_require_configuration() -> None:
    clean_env = {key: value for key, value in os.environ.items() if key not in _REQUIRED_ENV}
    for key in _REQUIRED_ENV:
        assert key not in clean_env
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.main; assert hasattr(app.main, 'create_app'); "
            "assert not hasattr(app.main, 'app'), 'module-level app was constructed'",
        ],
        cwd=API_ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_create_app_uses_explicit_settings(settings: Settings) -> None:
    application = create_app(settings)
    assert isinstance(application, FastAPI)
    assert application.state.settings is settings
    assert application.title == "AI API Gateway"


def test_lifespan_shares_clients_and_closes_them_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured: dict[str, Any] = {"redis": None, "litellm": None}

    class FakeRedis:
        closed = False

        async def aclose(self) -> None:
            type(self).closed = True

    class FakeHTTPClient:
        closed = False

        async def aclose(self) -> None:
            type(self).closed = True

    def redis_factory(_settings: Settings) -> FakeRedis:
        configured["redis"] = FakeRedis()
        return configured["redis"]

    def http_factory(*_args: Any, **_kwargs: Any) -> FakeHTTPClient:
        configured["litellm"] = FakeHTTPClient()
        return configured["litellm"]

    monkeypatch.setattr("app.main.create_redis_client", redis_factory)
    monkeypatch.setattr("app.main.httpx.AsyncClient", http_factory)
    monkeypatch.setattr(database_state, "init", _async_noop)
    monkeypatch.setattr(database_state, "dispose", _async_noop)

    application = create_app(settings)
    with TestClient(application):
        assert application.state.redis is configured["redis"]
        assert application.state.litellm_client is configured["litellm"]
        assert configured["redis"].closed is False
        assert configured["litellm"].closed is False

    assert configured["redis"].closed is True
    assert configured["litellm"].closed is True


def test_lifespan_does_not_reconfigure_logging(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("app.main.configure_logging", lambda level: calls.append(level))
    monkeypatch.setattr("app.main.create_redis_client", lambda _settings: _FakeClosable())
    monkeypatch.setattr("app.main.httpx.AsyncClient", lambda **_kwargs: _FakeClosable())
    monkeypatch.setattr(database_state, "init", _async_noop)
    monkeypatch.setattr(database_state, "dispose", _async_noop)

    application = create_app(settings)
    with TestClient(application):
        pass

    assert calls == [settings.log_level]


class _FakeClosable:
    async def aclose(self) -> None:
        return None


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None
