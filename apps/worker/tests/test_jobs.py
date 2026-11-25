"""Phase 1 job stub tests. No Redis, Postgres, or external provider is needed."""

import pytest

from app.jobs import (
    expire_reservations,
    process_webhook,
    reconciliation,
    send_email,
    settle_credits,
    usage_rollup,
)


@pytest.mark.asyncio
async def test_settle_credits_is_noop() -> None:
    result = await settle_credits({}, usage_event_id="usage-1")
    assert result == {"status": "noop", "job": "settle_credits"}


@pytest.mark.asyncio
async def test_process_webhook_is_noop() -> None:
    result = await process_webhook({}, event_id="event-1")
    assert result == {"status": "noop", "job": "process_webhook"}


@pytest.mark.asyncio
async def test_usage_rollup_is_noop() -> None:
    result = await usage_rollup(
        {},
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-01-02T00:00:00Z",
    )
    assert result == {"status": "noop", "job": "usage_rollup"}


@pytest.mark.asyncio
async def test_reconciliation_is_noop() -> None:
    result = await reconciliation({}, run_id="run-1")
    assert result == {"status": "noop", "job": "reconciliation"}


@pytest.mark.asyncio
async def test_expire_reservations_is_noop() -> None:
    result = await expire_reservations({}, batch_size=25)
    assert result == {"status": "noop", "job": "expire_reservations"}


@pytest.mark.asyncio
async def test_send_email_is_noop() -> None:
    result = await send_email(
        {},
        template="receipt",
        recipient_id="user-1",
        idempotency_key="email-1",
    )
    assert result == {"status": "noop", "job": "send_email"}
