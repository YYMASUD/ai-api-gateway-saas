"""Phase 4 configuration tests: env-only, validated bounds, no secret defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_phase4_defaults(settings: Settings) -> None:
    assert settings.max_output_tokens == 4096
    assert settings.max_request_bytes == 1048576
    assert settings.rate_limit_requests_per_minute == 60
    assert settings.rate_limit_max_concurrent == 8
    assert settings.reservation_ttl_seconds == 900


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_OUTPUT_TOKENS", "0"),
        ("MAX_OUTPUT_TOKENS", "128001"),
        ("MAX_REQUEST_BYTES", "1023"),
        ("RATE_LIMIT_REQUESTS_PER_MINUTE", "0"),
        ("RATE_LIMIT_MAX_CONCURRENT", "0"),
        ("RESERVATION_TTL_SECONDS", "0"),
        ("RESERVATION_TTL_SECONDS", "86401"),
    ],
)
def test_phase4_bounds_rejected(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("name", "setting", "value"),
    [
        ("MAX_OUTPUT_TOKENS", "max_output_tokens", "8192"),
        ("MAX_REQUEST_BYTES", "max_request_bytes", "2048"),
        ("RATE_LIMIT_REQUESTS_PER_MINUTE", "rate_limit_requests_per_minute", "120"),
        ("RATE_LIMIT_MAX_CONCURRENT", "rate_limit_max_concurrent", "32"),
        ("RESERVATION_TTL_SECONDS", "reservation_ttl_seconds", "300"),
    ],
)
def test_phase4_settings_read_from_environment(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    setting: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    assert getattr(Settings(), setting) == int(value)
