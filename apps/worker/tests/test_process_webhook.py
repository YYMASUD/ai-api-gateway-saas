"""Webhook settlement tests with an in-memory fake session -- no real DB or Redis."""

from __future__ import annotations

import importlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.errors import IdempotencyConflictError
from app.db.models import Payment, WebhookEvent
from app.services.credit_packs import MAX_CENTS
from structlog.testing import capture_logs

from worker_app.jobs import process_webhook

mod = importlib.import_module("worker_app.jobs.process_webhook")

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
PAYMENT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER_PAYMENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
SESSION_ID = "cs_test_123"
PAYMENT_INTENT = "pi_test_123"
AMOUNT_CENTS = 1_000
PACK_ID = "pack_10"
PACK_CREDITS = 10
EXPECTED_MICROCREDITS = 10_000_000


@dataclass
class FakeLedger:
    """Minimal stand-in for a credit_ledger row."""

    user_id: uuid.UUID
    amount_microcredits: int
    reference_id: str
    idempotency_key: str


class FakeResult:
    """The one result method the job uses."""

    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class FakeSession:
    """Answers only the selects the job issues and records commit order."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def execute(self, statement: Any) -> FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        values = list(params.values())
        if entity is WebhookEvent:
            return FakeResult(self.store.events.get(values[0]) if values else None)
        if entity is Payment:
            for key, value in params.items():
                if key.startswith("stripe_session_id"):
                    return self._first("stripe_session_id", value)
                if key.startswith("stripe_payment_intent"):
                    return self._first("stripe_payment_intent", value)
                if key.startswith("id"):
                    return self._first("id", value)
        message = f"unexpected select for {entity}"
        raise AssertionError(message)

    def _first(self, attribute: str, value: Any) -> FakeResult:
        for payment in self.store.payments:
            if getattr(payment, attribute) == value:
                return FakeResult(payment)
        return FakeResult(None)

    async def commit(self) -> None:
        self.store.commits.append(
            {
                "ledgers": list(self.store.ledgers),
                "payment_statuses": [item.status for item in self.store.payments],
                "processed_at": [item.processed_at for item in self.store.events.values()],
            }
        )

    async def rollback(self) -> None:
        self.store.rollbacks += 1

    async def grant_purchase(
        self,
        *,
        user_id: uuid.UUID,
        amount_microcredits: int,
        reference_id: str,
        idempotency_key: str,
    ) -> FakeLedger:
        """Mirror the real ledger key/fingerprint contract in memory."""
        self.store.grant_calls.append(
            {
                "user_id": user_id,
                "amount_microcredits": amount_microcredits,
                "reference_id": reference_id,
                "idempotency_key": idempotency_key,
            }
        )
        for row in self.store.ledgers:
            if row.idempotency_key == idempotency_key:
                fingerprint = (row.user_id, row.amount_microcredits, row.reference_id)
                expected = (user_id, amount_microcredits, reference_id)
                if fingerprint != expected:
                    raise IdempotencyConflictError(
                        "idempotency_key already used for a different purchase"
                    )
                return row
        ledger = FakeLedger(user_id, amount_microcredits, reference_id, idempotency_key)
        self.store.ledgers.append(ledger)
        return ledger


class FakeCreditService:
    """Routes ``grant_purchase`` to the in-memory fake session."""

    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def grant_purchase(
        self,
        session: FakeSession,
        *,
        user_id: uuid.UUID,
        amount_microcredits: int,
        reference_id: str,
        idempotency_key: str,
    ) -> FakeLedger:
        del session
        return await self._session.grant_purchase(
            user_id=user_id,
            amount_microcredits=amount_microcredits,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
        )


class FakeStore:
    """Shared state visible to every session_scope in one test."""

    def __init__(self) -> None:
        self.events: dict[uuid.UUID, WebhookEvent] = {}
        self.payments: list[Payment] = []
        self.ledgers: list[FakeLedger] = []
        self.grant_calls: list[dict[str, Any]] = []
        self.commits: list[dict[str, Any]] = []
        self.rollbacks = 0

    def new_session(self) -> FakeSession:
        return FakeSession(self)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()

    @asynccontextmanager
    async def fake_session_scope():  # type: ignore[no-untyped-def]
        session = fake.new_session()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()

    monkeypatch.setattr(mod, "session_scope", fake_session_scope)
    monkeypatch.setattr(mod, "CreditService", FakeCreditService)
    return fake


def make_event(
    *,
    event_type: str = "checkout.session.completed",
    payload: dict[str, Any] | None = None,
    processed_at: datetime | None = None,
    attempts: int = 0,
) -> WebhookEvent:
    return WebhookEvent(
        id=uuid.uuid4(),
        stripe_event_id="evt_test_123",
        type=event_type,
        payload=payload if payload is not None else {},
        processed_at=processed_at,
        attempts=attempts,
        error=None,
    )


def make_payment(
    *,
    payment_id: uuid.UUID = PAYMENT_ID,
    session_id: str | None = SESSION_ID,
    user_id: uuid.UUID = USER_ID,
    amount_cents: int = AMOUNT_CENTS,
    currency: str = "usd",
    payment_intent: str | None = None,
) -> Payment:
    return Payment(
        id=payment_id,
        user_id=user_id,
        stripe_session_id=session_id,
        stripe_payment_intent=payment_intent,
        amount_cents=amount_cents,
        credits_granted=0,
        status="pending",
        currency=currency,
    )


def make_payload(
    *,
    user_id: uuid.UUID | str | None = USER_ID,
    amount_total: object = AMOUNT_CENTS,
    session_id: object = SESSION_ID,
    payment_intent: object = PAYMENT_INTENT,
    credits: object = str(PACK_CREDITS),
    include_credits: bool = True,
    pack_id: object = PACK_ID,
    include_pack_id: bool = True,
    payment_status: object = "paid",
    include_payment_status: bool = True,
    currency: object = "usd",
    include_currency: bool = True,
    secrets: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"payment_id": str(PAYMENT_ID)}
    if user_id is not None:
        metadata["user_id"] = str(user_id)
    if include_credits:
        metadata["credits"] = credits
    if include_pack_id:
        metadata["pack_id"] = pack_id
    session_object: dict[str, Any] = {
        "id": session_id,
        "payment_intent": payment_intent,
        "amount_total": amount_total,
        "client_reference_id": str(user_id) if user_id is not None else None,
        "metadata": metadata,
    }
    if include_payment_status:
        session_object["payment_status"] = payment_status
    if include_currency:
        session_object["currency"] = currency
    if secrets:
        session_object["customer_details"] = {"email": "cardholder@example.test"}
        session_object["payment_method_details"] = {
            "card": {"number": "4242424242424242", "cvc": "123"}
        }
    return {
        "id": "evt_test_123",
        "type": "checkout.session.completed",
        "data": {"object": session_object},
    }


async def expect_failure(
    event: WebhookEvent,
    *,
    reason: str,
    exception: type[Exception] | None = None,
    match: str | None = None,
) -> None:
    expected = exception or mod.WebhookProcessingError
    with pytest.raises(expected, match=match if match is not None else reason):
        await process_webhook({}, event_id=str(event.id))
    assert event.attempts == 1
    assert event.error == reason
    assert event.processed_at is None


async def expect_payload_failure(store: FakeStore, payload: dict[str, Any], reason: str) -> None:
    event = make_event(payload=payload)
    payment = make_payment()
    store.events[event.id] = event
    store.payments.append(payment)

    await expect_failure(event, reason=reason)

    assert not store.ledgers
    assert not store.grant_calls
    assert payment.status == "pending"
    assert payment.credits_granted == 0
    assert payment.stripe_payment_intent is None


@pytest.mark.asyncio
async def test_paid_checkout_grants_and_marks_payment(store: FakeStore) -> None:
    event = make_event(payload=make_payload())
    payment = make_payment()
    store.events[event.id] = event
    store.payments.append(payment)

    result = await process_webhook({}, event_id=str(event.id))

    assert result["status"] == "processed"
    assert result["amount_microcredits"] == EXPECTED_MICROCREDITS
    assert len(store.ledgers) == 1
    ledger = store.ledgers[0]
    assert ledger.user_id == USER_ID
    assert ledger.amount_microcredits == EXPECTED_MICROCREDITS
    assert ledger.reference_id == SESSION_ID
    assert ledger.idempotency_key == f"stripe:{SESSION_ID}"
    assert payment.status == "completed"
    assert payment.stripe_payment_intent == PAYMENT_INTENT
    assert payment.amount_cents == AMOUNT_CENTS
    assert payment.credits_granted == EXPECTED_MICROCREDITS
    assert event.processed_at is not None
    assert event.error is None
    assert len(store.commits) == 1
    snapshot = store.commits[0]
    assert snapshot["ledgers"] == [ledger]
    assert snapshot["payment_statuses"] == ["completed"]
    assert snapshot["processed_at"] == [event.processed_at]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        make_payload(payment_status="unpaid"),
        make_payload(payment_status="no_payment_required"),
        make_payload(include_payment_status=False),
    ],
)
async def test_unpaid_checkout_is_retryable_without_grant(
    store: FakeStore, payload: dict[str, Any]
) -> None:
    await expect_payload_failure(store, payload, "payment not paid")


@pytest.mark.asyncio
async def test_async_payment_succeeded_paid_grants(store: FakeStore) -> None:
    event = make_event(
        event_type="checkout.session.async_payment_succeeded", payload=make_payload()
    )
    store.events[event.id] = event
    store.payments.append(make_payment())

    result = await process_webhook({}, event_id=str(event.id))

    assert result["status"] == "processed"
    assert len(store.ledgers) == 1
    assert event.processed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["eur", "gbp", "USDX", "", None])
async def test_non_usd_currency_rejected(store: FakeStore, currency: object) -> None:
    await expect_payload_failure(store, make_payload(currency=currency), "invalid currency")


@pytest.mark.asyncio
async def test_usd_currency_is_case_and_space_insensitive(store: FakeStore) -> None:
    event = make_event(payload=make_payload(currency=" USD "))
    store.events[event.id] = event
    store.payments.append(make_payment())

    result = await process_webhook({}, event_id=str(event.id))

    assert result["status"] == "processed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount_total", "reason"),
    [
        (1_234, "amount_total not in catalog"),
        (MAX_CENTS + 1, "amount_total exceeds maximum"),
    ],
)
async def test_amount_outside_catalog_rejected(
    store: FakeStore, amount_total: int, reason: str
) -> None:
    await expect_payload_failure(store, make_payload(amount_total=amount_total), reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (make_payload(include_credits=False), "missing metadata credits"),
        (make_payload(credits="9999"), "metadata credits mismatch"),
        (make_payload(credits="1"), "metadata credits mismatch"),
        (make_payload(credits=str(EXPECTED_MICROCREDITS)), "metadata credits mismatch"),
        (make_payload(credits=7.5), "metadata credits mismatch"),
        (make_payload(credits=""), "metadata credits mismatch"),
        (make_payload(pack_id="pack_100"), "metadata pack id mismatch"),
    ],
)
async def test_metadata_is_strict(store: FakeStore, payload: dict[str, Any], reason: str) -> None:
    await expect_payload_failure(store, payload, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("amount_total", ["1000", 0, -50, True, None])
async def test_invalid_amount_total_rejected(store: FakeStore, amount_total: object) -> None:
    await expect_payload_failure(
        store, make_payload(amount_total=amount_total), "invalid amount_total"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "", "not-a-uuid"])
async def test_invalid_user_id_rejected(store: FakeStore, user_id: object) -> None:
    reason = "missing user id" if user_id in (None, "") else "invalid user id"
    await expect_payload_failure(store, make_payload(user_id=user_id), reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payment", "reason"),
    [
        (make_payment(user_id=OTHER_USER_ID), "payment user mismatch"),
        (make_payment(amount_cents=500), "payment amount mismatch"),
        (make_payment(session_id="cs_other", payment_id=PAYMENT_ID), "payment session mismatch"),
        (make_payment(currency="eur"), "payment currency mismatch"),
    ],
)
async def test_payment_consistency_failures_leave_payment_untouched(
    store: FakeStore, payment: Payment, reason: str
) -> None:
    event = make_event(payload=make_payload())
    store.events[event.id] = event
    store.payments.append(payment)
    original_amount = payment.amount_cents

    await expect_failure(event, reason=reason)

    assert not store.ledgers
    assert not store.grant_calls
    assert payment.status == "pending"
    assert payment.credits_granted == 0
    assert payment.stripe_payment_intent is None
    assert payment.amount_cents == original_amount


@pytest.mark.asyncio
async def test_session_id_longer_than_column_rejected(store: FakeStore) -> None:
    payload = make_payload(session_id="cs_" + "x" * 130)
    await expect_payload_failure(store, payload, "session id too long")


@pytest.mark.asyncio
async def test_payment_intent_collision_rejected_before_unique_violation(
    store: FakeStore,
) -> None:
    owner = make_payment(
        payment_id=OTHER_PAYMENT_ID,
        session_id="cs_other",
        payment_intent=PAYMENT_INTENT,
    )
    target = make_payment(payment_intent=None)
    event = make_event(payload=make_payload())
    store.events[event.id] = event
    store.payments.extend([owner, target])

    await expect_failure(event, reason="payment intent collision")

    assert not store.ledgers
    assert not store.grant_calls
    assert target.status == "pending"
    assert target.stripe_payment_intent is None
    assert target.credits_granted == 0


@pytest.mark.asyncio
async def test_replay_returns_already_processed_without_second_grant(store: FakeStore) -> None:
    event = make_event(payload=make_payload())
    store.events[event.id] = event
    store.payments.append(make_payment())

    first = await process_webhook({}, event_id=str(event.id))
    event.processed_at = datetime.now(UTC)
    second = await process_webhook({}, event_id=str(event.id))

    assert first["status"] == "processed"
    assert second == {"status": "already_processed"}
    assert len(store.ledgers) == 1
    assert len(store.grant_calls) == 1


@pytest.mark.asyncio
async def test_second_checkout_event_for_same_session_uses_ledger_key(store: FakeStore) -> None:
    first_event = make_event(payload=make_payload())
    second_event = make_event(payload=make_payload())
    store.events[first_event.id] = first_event
    store.events[second_event.id] = second_event
    store.payments.append(make_payment())

    assert (await process_webhook({}, event_id=str(first_event.id)))["status"] == "processed"
    assert (await process_webhook({}, event_id=str(second_event.id)))["status"] == "processed"

    assert len(store.ledgers) == 1
    assert len(store.grant_calls) == 2
    assert {call["idempotency_key"] for call in store.grant_calls} == {f"stripe:{SESSION_ID}"}


@pytest.mark.asyncio
async def test_missing_event_returns_skipped(store: FakeStore) -> None:
    result = await process_webhook({}, event_id=str(uuid.uuid4()))

    assert result == {"status": "skipped", "reason": "event_not_found"}
    assert not store.ledgers
    assert not store.grant_calls


@pytest.mark.asyncio
async def test_unknown_type_is_marked_processed_and_ignored(store: FakeStore) -> None:
    event = make_event(event_type="payment_intent.succeeded", payload=make_payload())
    store.events[event.id] = event

    result = await process_webhook({}, event_id=str(event.id))

    assert result == {"status": "ignored", "type": "payment_intent.succeeded"}
    assert event.processed_at is not None
    assert event.error is None
    assert not store.ledgers
    assert not store.grant_calls


@pytest.mark.asyncio
async def test_payment_intent_and_payment_id_lookup_are_persisted(store: FakeStore) -> None:
    event = make_event(payload=make_payload(payment_intent="pi_attached_456"))
    payment = make_payment(session_id=None)
    store.events[event.id] = event
    store.payments.append(payment)

    result = await process_webhook({}, event_id=str(event.id))

    assert result["status"] == "processed"
    assert payment.stripe_payment_intent == "pi_attached_456"
    assert payment.stripe_session_id is None
    assert payment.status == "completed"
    assert store.grant_calls[0]["reference_id"] == SESSION_ID


@pytest.mark.asyncio
async def test_ledger_idempotency_conflict_fails_event(store: FakeStore) -> None:
    store.ledgers.append(FakeLedger(USER_ID, 99, SESSION_ID, f"stripe:{SESSION_ID}"))
    event = make_event(payload=make_payload())
    store.events[event.id] = event
    store.payments.append(make_payment())

    await expect_failure(
        event,
        reason="processing failed",
        exception=IdempotencyConflictError,
        match="idempotency_key already used",
    )

    assert len(store.grant_calls) == 1
    assert len(store.ledgers) == 1


@pytest.mark.asyncio
async def test_logs_never_include_raw_payload_or_card_data(store: FakeStore) -> None:
    event = make_event(payload=make_payload(secrets=True))
    store.events[event.id] = event
    store.payments.append(make_payment())

    with capture_logs() as logs:
        result = await process_webhook({}, event_id=str(event.id))

    assert result["status"] == "processed"
    serialized = repr(logs)
    assert "4242424242424242" not in serialized
    assert "cardholder@example.test" not in serialized
    assert "cvc" not in serialized
    assert SESSION_ID not in serialized
    assert PAYMENT_INTENT not in serialized
    assert any(
        entry.get("event") == "webhook"
        and entry.get("event_id") == str(event.id)
        and entry.get("type") == "checkout.session.completed"
        and entry.get("outcome") == "processed"
        for entry in logs
    )
