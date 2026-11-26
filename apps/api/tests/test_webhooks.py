"""Webhook ingestion tests (mocked Stripe signature and persistence, no DB)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import WebhookSignatureError
from app.db.models import CreditLedger
from app.db.session import get_db
from app.routers.webhooks import get_webhook_event_store
from app.services.job_queue import get_job_queue


@pytest.fixture(autouse=True)
def _configure_webhook_secret(app: FastAPI) -> None:
    """Use a fake non-secret webhook secret for signature verification tests."""
    app.state.settings.stripe_webhook_secret = "whsec_test_not_a_real_secret"


class FakeJobQueue:
    """Records enqueued jobs for assertion."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, **kwargs: Any) -> None:
        self.jobs.append({"function": function, "kwargs": kwargs})


class FakeWebhookEventStore:
    """In-memory store simulating ON CONFLICT DO NOTHING plus outbox commits."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.insert_calls: list[dict[str, Any]] = []
        self.commit_calls = 0
        self.committed_ids: set[str] = set()

    async def insert_if_new(
        self, *, stripe_event_id: str, event_type: str, payload: dict[str, Any]
    ) -> uuid.UUID | None:
        self.insert_calls.append(
            {
                "stripe_event_id": stripe_event_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        if stripe_event_id in self.events:
            return None
        row_id = uuid.uuid4()
        self.events[stripe_event_id] = {
            "id": row_id,
            "stripe_event_id": stripe_event_id,
            "type": event_type,
            "payload": payload,
            "processed_at": None,
        }
        return row_id

    async def commit(self) -> None:
        self.commit_calls += 1
        self.committed_ids = set(self.events)

    async def get_by_stripe_event_id(self, stripe_event_id: str) -> Any:
        row = self.events.get(stripe_event_id)
        if row is None:
            return None
        return type(
            "EventRow",
            (),
            {
                "id": row["id"],
                "stripe_event_id": row["stripe_event_id"],
                "type": row["type"],
                "payload": row["payload"],
                "processed_at": row.get("processed_at"),
            },
        )()


class _FakeResult:
    def __init__(self, value: uuid.UUID | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return self._value


class _LedgerSpySession:
    """Session double that records any ORM adds and simulates ON CONFLICT."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.seen_ids: set[str] = set()

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def execute(self, stmt: Any) -> _FakeResult:
        values = stmt._values
        raw = next(
            value
            for column, value in values.items()
            if getattr(column, "name", None) == "stripe_event_id"
        )
        event_id = getattr(raw, "value", raw)
        if event_id in self.seen_ids:
            return _FakeResult(None)
        self.seen_ids.add(event_id)
        return _FakeResult(uuid.uuid4())

    async def commit(self) -> None:
        return None


def _client(app: FastAPI, store: FakeWebhookEventStore, job_queue: FakeJobQueue) -> TestClient:
    app.dependency_overrides[get_webhook_event_store] = lambda: store
    app.dependency_overrides[get_job_queue] = lambda: job_queue
    return TestClient(app, raise_server_exceptions=False)


def _make_event_payload(event_id: str | None = None) -> dict[str, Any]:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "type": "checkout.session.completed",
        "data": {"object": {"id": f"cs_{uuid.uuid4().hex}"}},
    }


def _patch_event(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_id: str,
    event_type: str = "checkout.session.completed",
    payload: dict[str, Any] | None = None,
) -> None:
    event_payload = payload or {"id": event_id, "type": event_type}

    def _fake_construct_event(raw_body: bytes, signature: str, secret: str) -> Any:
        assert isinstance(raw_body, bytes)
        return type(
            "Event",
            (),
            {"id": event_id, "type": event_type, "to_dict": lambda *args, **kwargs: event_payload},
        )()

    monkeypatch.setattr("app.routers.webhooks.construct_event", _fake_construct_event)


def test_webhook_invalid_signature_returns_400(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    def _raise_signature_error(*args: Any, **kwargs: Any) -> Any:
        raise WebhookSignatureError("Invalid signature")

    monkeypatch.setattr("app.routers.webhooks.construct_event", _raise_signature_error)

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(_make_event_payload()).encode(),
        headers={"stripe-signature": "bogus"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    assert len(store.events) == 0
    assert len(job_queue.jobs) == 0


def test_webhook_happy_path_records_and_enqueues(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    _patch_event(monkeypatch, event_id=event_id, payload=payload)

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}

    assert len(store.events) == 1
    row = store.events[event_id]
    assert row["type"] == "checkout.session.completed"
    assert row["payload"] == payload

    assert len(job_queue.jobs) == 1
    assert job_queue.jobs[0]["function"] == "process_webhook"
    # The API enqueues only the opaque local webhook-event UUID; the worker
    # fetches customer payload data from storage.
    assert job_queue.jobs[0]["kwargs"]["event_id"] == str(row["id"])


def test_webhook_replay_same_event_five_times(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unprocessed replays re-enqueue for recovery; processed replays do not.

    Five rapid deliveries before the worker finishes produce one event row and
    five recovery enqueues; the worker/ledger still grant exactly once. Once the
    event is marked processed, further deliveries return already_processed with
    no new enqueue.
    """
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    _patch_event(monkeypatch, event_id=event_id, payload=payload)

    responses = [
        client.post(
            "/webhooks/stripe",
            content=json.dumps(payload).encode(),
            headers={"stripe-signature": "valid_sig"},
        )
        for _ in range(5)
    ]

    assert all(response.status_code == 200 for response in responses)
    assert all(r.json() == {"status": "queued"} for r in responses)
    assert len(store.events) == 1
    assert len(job_queue.jobs) == 5

    store.events[event_id]["processed_at"] = "2026-01-01T00:00:00Z"
    replay = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert replay.status_code == 200
    assert replay.json() == {"status": "already_processed"}
    assert len(job_queue.jobs) == 5


def test_webhook_ignored_event_type_recorded_but_not_enqueued(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    _patch_event(
        monkeypatch, event_id=event_id, event_type="payment_intent.succeeded", payload=payload
    )

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert len(store.events) == 1
    assert store.events[event_id]["type"] == "payment_intent.succeeded"
    assert len(job_queue.jobs) == 0


def test_webhook_never_grants_credits_inline(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid completion only stores the event and enqueues the worker.

    ``FakeWebhookEventStore`` has no credit-ledger API; a route attempting to
    grant credits inline could not write any ledger row. Every value passed to
    the store contains only webhook-event fields, never credit fields.
    """
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    _patch_event(monkeypatch, event_id=event_id, payload=payload)

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert response.status_code == 200
    assert len(store.insert_calls) == 1
    assert set(store.insert_calls[0]) == {"stripe_event_id", "event_type", "payload"}
    assert len(job_queue.jobs) == 1


def test_webhook_replay_does_not_write_credit_ledger(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five unprocessed deliveries never grant credits in the API layer."""
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    _patch_event(monkeypatch, event_id=event_id, payload=payload)

    for _ in range(5):
        response = client.post(
            "/webhooks/stripe",
            content=json.dumps(payload).encode(),
            headers={"stripe-signature": "valid_sig"},
        )
        assert response.status_code == 200

    assert len(job_queue.jobs) == 5
    assert not hasattr(store, "create_ledger")


def test_webhook_unknown_event_id_replay_is_enforced(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    _patch_event(monkeypatch, event_id=event_id, payload=payload)

    first = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    replay = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert first.json() == {"status": "queued"}
    # Unprocessed replay is a recovery re-enqueue, not a duplicate event row.
    assert replay.json() == {"status": "queued"}
    assert len(store.events) == 1

    store.events[event_id]["processed_at"] = "2026-01-01T00:00:00Z"
    done = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert done.json() == {"status": "already_processed"}


def test_webhook_persists_raw_event_payload(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    payload["data"]["object"]["customer"] = "cus_test_private"
    _patch_event(monkeypatch, event_id=event_id, payload=payload)

    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert response.status_code == 200
    assert store.events[event_id]["payload"] == payload
    assert "cus_test_private" not in response.text


def test_webhook_requires_signature_header_and_secret(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeWebhookEventStore()
    job_queue = FakeJobQueue()
    client = _client(app, store, job_queue)
    app.state.settings.stripe_webhook_secret = ""

    response = client.post(
        "/webhooks/stripe",
        content=b"{}",
        headers={"stripe-signature": "bogus"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    assert len(store.events) == 0


def test_webhook_route_never_writes_credit_ledger_rows(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the real event store, a completed checkout writes no ledger row."""
    session = _LedgerSpySession()
    job_queue = FakeJobQueue()

    async def _fake_db() -> Any:
        yield session

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_job_queue] = lambda: job_queue

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = _make_event_payload(event_id)
    _patch_event(monkeypatch, event_id=event_id, payload=payload)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/webhooks/stripe",
        content=json.dumps(payload).encode(),
        headers={"stripe-signature": "valid_sig"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert session.seen_ids == {event_id}
    assert not any(isinstance(obj, CreditLedger) for obj in session.added)
    assert len(job_queue.jobs) == 1
