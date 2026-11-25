"""Configuration parsing tests: env-only, no secret defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_read_from_environment(env: dict[str, str]) -> None:
    settings = Settings()
    assert settings.app_url == "https://dashboard.example.com"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.redis_url == "redis://cache:6379/0"
    assert settings.litellm_base_url == "http://litellm:4000"
    assert settings.jwt_issuer == "ai-api-gateway"
    assert settings.jwt_audience == "ai-api-gateway-dashboard"


def test_cors_origins_derived_from_app_url(settings: Settings) -> None:
    assert settings.cors_origins == ["https://dashboard.example.com"]


def test_cors_origins_uses_origin_only(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_URL", "https://app.example.com/some/path?x=1")
    assert Settings().cors_origins == ["https://app.example.com"]


def test_trailing_slashes_are_stripped(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_URL", "https://dashboard.example.com/")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm:4000/")
    settings = Settings()
    assert settings.app_url == "https://dashboard.example.com"
    assert settings.litellm_base_url == "http://litellm:4000"


@pytest.mark.parametrize(
    "missing",
    ["DATABASE_URL", "REDIS_URL", "LITELLM_MASTER_KEY", "JWT_SECRET", "API_KEY_PEPPER"],
)
def test_missing_secret_environment_variable_fails(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("variable", ["DATABASE_URL", "JWT_SECRET", "API_KEY_PEPPER"])
def test_blank_secret_is_rejected(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, "   ")
    with pytest.raises(ValidationError):
        Settings()


def test_timeouts_default_to_connect_five_read_three_hundred(settings: Settings) -> None:
    assert settings.http_connect_timeout == 5.0
    assert settings.http_read_timeout == 300.0


def test_non_absolute_app_url_rejected(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_URL", "dashboard.example.com")
    with pytest.raises(ValueError):
        _ = Settings().cors_origins


@pytest.mark.parametrize("algorithm", ["HS512", "RS256", "none"])
def test_jwt_algorithm_must_be_hs256(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, algorithm: str
) -> None:
    monkeypatch.setenv("JWT_ALGORITHM", algorithm)
    with pytest.raises(ValidationError):
        Settings()


def test_log_level_is_normalised(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"


def test_timeouts_must_be_positive(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_CONNECT_TIMEOUT", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_have_no_secret_defaults() -> None:
    for field in (
        "database_url",
        "redis_url",
        "litellm_master_key",
        "jwt_secret",
        "api_key_pepper",
    ):
        assert Settings.model_fields[field].is_required()
