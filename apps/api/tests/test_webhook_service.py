"""WebhookEventStore SQL idempotency tests (no DB, no network)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects import postgresql

from app.db.models import WebhookEvent
from app.services.webhook_service import SQLAlchemyWebhookEventStore


class _FakeResult:
    def __init__(self, value: uuid.UUID | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return self._value


class _RecordingSession:
    """Minimal async session stand-in capturing ON CONFLICT behavior."""

    def __init__(self) -> None:
        self.seen_ids: set[str] = set()
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        # Extract stripe_event_id from the statement values dict.
        values = stmt._values
        event_id = next(
            v for k, v in values.items() if getattr(k, "name", None) == "stripe_event_id"
        )
        # ``stmt.values()`` stores BindParameter objects, not raw literals.
        event_id = getattr(event_id, "value", event_id)
        if event_id in self.seen_ids:
            return _FakeResult(None)
        self.seen_ids.add(event_id)
        return _FakeResult(uuid.uuid4())


def _compiled(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


async def test_insert_if_new_returns_uuid_on_first_call() -> None:
    session = _RecordingSession()
    store = SQLAlchemyWebhookEventStore(session)
    row_id = await store.insert_if_new(
        stripe_event_id="evt_first",
        event_type="checkout.session.completed",
        payload={"data": {"object": {"id": "cs_1"}}},
    )
    assert isinstance(row_id, uuid.UUID)
    assert len(session.executed) == 1
    # The compiled SQL must use ON CONFLICT on stripe_event_id.
    compiled = _compiled(session.executed[0])
    assert "ON CONFLICT (stripe_event_id) DO NOTHING" in compiled
    assert "RETURNING webhook_events.id" in compiled


async def test_insert_if_new_returns_none_on_replay() -> None:
    session = _RecordingSession()
    store = SQLAlchemyWebhookEventStore(session)
    first = await store.insert_if_new(
        stripe_event_id="evt_replay",
        event_type="checkout.session.completed",
        payload={"data": {}},
    )
    second = await store.insert_if_new(
        stripe_event_id="evt_replay",
        event_type="checkout.session.completed",
        payload={"data": {}},
    )
    assert isinstance(first, uuid.UUID)
    assert second is None
    assert len(session.seen_ids) == 1


async def test_distinct_event_ids_both_insert() -> None:
    session = _RecordingSession()
    store = SQLAlchemyWebhookEventStore(session)
    a = await store.insert_if_new(
        stripe_event_id="evt_a",
        event_type="checkout.session.completed",
        payload={"data": {}},
    )
    b = await store.insert_if_new(
        stripe_event_id="evt_b",
        event_type="checkout.session.completed",
        payload={"data": {}},
    )
    assert isinstance(a, uuid.UUID)
    assert isinstance(b, uuid.UUID)
    assert a != b
    assert session.seen_ids == {"evt_a", "evt_b"}


def test_model_unique_constraint_on_stripe_event_id() -> None:
    """The schema-level unique constraint is enforced independently of SQL."""
    constraints = {getattr(c, "name", None) for c in WebhookEvent.__table__.constraints}
    assert "uq_webhook_events_stripe_event_id" in constraints
