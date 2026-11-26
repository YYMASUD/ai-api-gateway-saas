"""Idempotent ``usage_events`` persistence.

One row per gateway request, keyed on the sanitized ``request_id``. The
unique constraint on ``request_id`` plus ``ON CONFLICT DO NOTHING`` makes the
insert idempotent: a replayed request leaves the original row intact.

Only token counts, cost, latency, upstream provider, and error diagnostics
are stored. Prompts, completions, and message content are NEVER persisted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UsageEvent

__all__ = ["UsageRecord", "record_usage_event"]

_ALLOWED_STATUSES = frozenset({"success", "error", "partial"})


@dataclass(frozen=True)
class UsageRecord:
    """Content-free usage telemetry for a single gateway request."""

    user_id: str
    api_key_id: str | None
    request_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_microcredits: int
    status: str
    latency_ms: int | None
    upstream_provider: str | None
    total_tokens: int
    error_code: str | None
    metadata: dict[str, Any] | None = None


async def record_usage_event(session: AsyncSession, record: UsageRecord) -> None:
    """Idempotently insert a ``usage_events`` row; caller commits afterwards."""
    if record.status not in _ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {sorted(_ALLOWED_STATUSES)}")
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.UUID(record.user_id),
        "api_key_id": uuid.UUID(record.api_key_id) if record.api_key_id else None,
        "request_id": record.request_id,
        "model": record.model,
        "prompt_tokens": max(0, record.prompt_tokens),
        "completion_tokens": max(0, record.completion_tokens),
        "cost_microcredits": max(0, record.cost_microcredits),
        "status": record.status,
        "latency_ms": record.latency_ms,
        "upstream_provider": record.upstream_provider,
        "total_tokens": max(0, record.total_tokens),
        "error_code": record.error_code,
        # Core-table insert uses the physical column name ``metadata`` (the
        # ORM declares it as ``metadata_json`` because ``metadata`` is reserved).
        "metadata": record.metadata or {},
        "created_at": now,
        "updated_at": now,
    }
    # Insert against the Core table so the literal column name ``metadata`` is
    # used. Passing the attribute name to an ORM insert would resolve
    # ``metadata`` to the declarative MetaData object instead of the column.
    stmt = (
        pg_insert(cast(Table, UsageEvent.__table__))
        .values(**values)
        .on_conflict_do_nothing(index_elements=["request_id"])
    )
    await session.execute(stmt)
