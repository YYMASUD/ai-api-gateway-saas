"""Tests for content-free, idempotent usage-event persistence."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.services.usage import UsageRecord, record_usage_event

SECRET = "NEVER-STORE-THIS-MESSAGE-CONTENT-9f3a"


class _CapturingSession:
    """Captures statements without touching a database."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)


def _record(**overrides: Any) -> UsageRecord:
    values: dict[str, Any] = {
        "user_id": str(uuid.uuid4()),
        "api_key_id": str(uuid.uuid4()),
        "request_id": "req-1",
        "model": "gateway-default",
        "prompt_tokens": 8,
        "completion_tokens": 3,
        "cost_microcredits": 2,
        "status": "success",
        "latency_ms": 12,
        "upstream_provider": "litellm",
        "total_tokens": 11,
        "error_code": None,
    }
    values.update(overrides)
    return UsageRecord(**values)


async def test_record_usage_event_is_idempotent_insert() -> None:
    session = _CapturingSession()
    await record_usage_event(session, _record())  # type: ignore[arg-type]
    await record_usage_event(session, _record())  # type: ignore[arg-type]
    assert len(session.statements) == 2
    for statement in session.statements:
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        assert "INSERT INTO usage_events" in compiled
        assert "ON CONFLICT (request_id) DO NOTHING" in compiled


async def test_usage_record_has_no_content_fields() -> None:
    record = _record()
    assert not hasattr(record, "content")
    assert not hasattr(record, "messages")
    assert not hasattr(record, "prompt")
    assert set(record.__dataclass_fields__) == {
        "user_id",
        "api_key_id",
        "request_id",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "cost_microcredits",
        "status",
        "latency_ms",
        "upstream_provider",
        "total_tokens",
        "error_code",
        "metadata",
    }


async def test_usage_sql_params_contain_no_prompt_content() -> None:
    session = _CapturingSession()
    await record_usage_event(session, _record(metadata={"request_id": SECRET}))  # type: ignore[arg-type]
    statement = session.statements[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert compiled.params["metadata"] == {"request_id": SECRET}
    # No dedicated content/prompt column is ever bound.
    assert "content" not in {key.lower() for key in compiled.params}
    assert "prompt" not in {key.lower() for key in compiled.params}


@pytest.mark.parametrize("status", ["success", "partial", "error"])
async def test_usage_record_accepts_documented_statuses(status: str) -> None:
    session = _CapturingSession()
    await record_usage_event(session, _record(status=status))  # type: ignore[arg-type]
    assert len(session.statements) == 1


async def test_usage_record_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        await record_usage_event(_CapturingSession(), _record(status="unknown"))  # type: ignore[arg-type]
