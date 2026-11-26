"""Idempotent webhook event persistence.

The API records each verified Stripe event exactly once using a PostgreSQL
``INSERT ... ON CONFLICT DO NOTHING`` on the unique ``stripe_event_id`` column.
Credit granting deliberately lives in the worker, not here.

Events are global (not user-scoped): a lookup by Stripe event id must therefore
never filter on a user. The API commits the row before enqueueing the worker job
so the worker can always find it, and replays of unprocessed rows are safe to
enqueue again.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WebhookEvent

__all__ = ["SQLAlchemyWebhookEventStore", "WebhookEventStore"]


class WebhookEventStore(Protocol):
    """Persistence boundary for verified webhook events."""

    async def insert_if_new(
        self, *, stripe_event_id: str, event_type: str, payload: dict[str, Any]
    ) -> uuid.UUID | None:
        """Insert an event, returning its id, or ``None`` if already recorded."""
        ...

    async def commit(self) -> None:
        """Commit the event row so an enqueued worker can read it immediately."""
        ...

    async def get_by_stripe_event_id(self, stripe_event_id: str) -> WebhookEvent | None:
        """Return the global event row (including processing state), if any."""
        ...


class SQLAlchemyWebhookEventStore:
    """SQLAlchemy implementation using an atomic conflict-safe insert."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_if_new(
        self, *, stripe_event_id: str, event_type: str, payload: dict[str, Any]
    ) -> uuid.UUID | None:
        stmt = (
            pg_insert(WebhookEvent)
            .values(
                id=uuid.uuid4(),
                stripe_event_id=stripe_event_id,
                type=event_type,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["stripe_event_id"])
            .returning(WebhookEvent.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def commit(self) -> None:
        await self._session.commit()

    async def get_by_stripe_event_id(self, stripe_event_id: str) -> WebhookEvent | None:
        """Look up an event by its Stripe id, without any user/tenant filter."""
        stmt = select(WebhookEvent).where(WebhookEvent.stripe_event_id == stripe_event_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
