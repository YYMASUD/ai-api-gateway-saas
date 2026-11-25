"""Environment parsing tests for the worker configuration."""

import pytest
from pydantic import ValidationError

from app.config import WorkerSettings


def test_redis_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings()  # type: ignore[call-arg]


def test_settings_parse_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "rediss://redis.internal:6380/2")
    monkeypatch.setenv("WORKER_QUEUE_NAME", "private:jobs")
    monkeypatch.setenv("WORKER_MAX_JOBS", "4")
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("WORKER_RESULT_TTL_SECONDS", "90")
    monkeypatch.setenv("WORKER_LOG_LEVEL", "WARNING")

    settings = WorkerSettings()  # type: ignore[call-arg]

    assert str(settings.redis_url) == "rediss://redis.internal:6380/2"
    assert settings.queue_name == "private:jobs"
    assert settings.max_jobs == 4
    assert settings.job_timeout_seconds == 45
    assert settings.result_ttl_seconds == 90
    assert settings.log_level == "WARNING"


def test_non_secret_overrides_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://redis.internal:6379/0")
    monkeypatch.setenv("WORKER_MAX_JOBS", "0")

    with pytest.raises(ValidationError):
        WorkerSettings()  # type: ignore[call-arg]
