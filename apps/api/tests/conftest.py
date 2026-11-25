"""Shared test fixtures. No external services are contacted."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import Settings, get_settings

TEST_ENV = {
    "ENVIRONMENT": "test",
    "APP_URL": "https://dashboard.example.com",
    "API_BASE_URL": "https://api.example.com",
    "DATABASE_URL": "postgresql+asyncpg://gateway:not-a-real-password@db:5432/gateway",
    "REDIS_URL": "redis://cache:6379/0",
    "LITELLM_BASE_URL": "http://litellm:4000",
    "LITELLM_MASTER_KEY": "test-master-key-value",
    "JWT_SECRET": "test-jwt-secret-value",
    "JWT_ISSUER": "ai-api-gateway",
    "JWT_AUDIENCE": "ai-api-gateway-dashboard",
    "API_KEY_PEPPER": "test-pepper-value",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    # Drop any settings memoised before the environment was set, and again
    # afterwards, so stale settings never leak between tests.
    get_settings.cache_clear()
    try:
        yield dict(TEST_ENV)
    finally:
        get_settings.cache_clear()


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return Settings()


@pytest.fixture
def app(settings: Settings):
    from app.main import create_app

    application = create_app(settings)
    application.state.settings = settings
    return application
