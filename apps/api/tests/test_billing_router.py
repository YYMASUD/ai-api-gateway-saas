"""Billing router endpoint tests (mocked Stripe, no real DB)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.auth import get_current_user
from app.core.security import create_access_token
from app.db.models import Payment, User
from app.db.session import get_db
from app.routers.billing import get_stripe_service
from tests.fakes import FakeSession, make_user


class FakeStripeService:
    """Test double for StripeService; records calls and can simulate failures."""

    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.checkout_calls: list[dict[str, Any]] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def create_checkout_session(
        self,
        *,
        user_id: uuid.UUID,
        payment_id: uuid.UUID,
        pack: Any,
        success_url: str,
        cancel_url: str,
    ) -> Any:
        self.checkout_calls.append(
            {
                "user_id": user_id,
                "payment_id": payment_id,
                "pack": pack,
                "success_url": success_url,
                "cancel_url": cancel_url,
            }
        )
        if self.fail:
            raise RuntimeError("Stripe API unavailable")
        return SimpleNamespace(
            id=f"cs_test_{uuid.uuid4().hex[:8]}",
            url="https://checkout.stripe.com/c/pay/test_session",
        )


class FakeDBSession:
    """Minimal async session for testing payment creation."""

    def __init__(self) -> None:
        self.payments: list[Payment] = []
        self.committed = False
        self.rolled_back = False

    def add(self, obj: Any) -> None:
        if isinstance(obj, Payment):
            self.payments.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _client(
    app: FastAPI,
    user: User,
    stripe_service: FakeStripeService,
    db_session: FakeDBSession,
) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_stripe_service] = lambda: stripe_service

    async def _fake_db() -> AsyncIterator[FakeDBSession]:
        yield db_session

    app.dependency_overrides[get_db] = _fake_db
    return TestClient(app, raise_server_exceptions=False)


def _token(settings: Settings, user: User) -> str:
    token, _ = (
        create_access_token(
            str(user.id),
            settings.jwt_secret,
            settings.jwt_issuer,
            settings.jwt_audience,
            settings.jwt_access_token_expire_minutes,
        ),
        settings.jwt_access_token_expire_minutes * 60,
    )
    return token


def test_checkout_requires_authentication(app: FastAPI) -> None:
    async def _fake_db() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    app.dependency_overrides[get_db] = _fake_db
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/billing/checkout", json={"pack_id": "pack_10"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_checkout_returns_503_when_stripe_disabled(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    stripe_service = FakeStripeService(enabled=False)
    db_session = FakeDBSession()
    client = _client(app, user, stripe_service, db_session)

    response = client.post(
        "/billing/checkout",
        json={"pack_id": "pack_10"},
        headers={"Authorization": f"Bearer {_token(settings, user)}"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "stripe_not_configured"
    assert len(db_session.payments) == 0


def test_checkout_rejects_unknown_pack_id(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    stripe_service = FakeStripeService(enabled=True)
    db_session = FakeDBSession()
    client = _client(app, user, stripe_service, db_session)

    response = client.post(
        "/billing/checkout",
        json={"pack_id": "pack_999"},
        headers={"Authorization": f"Bearer {_token(settings, user)}"},
    )
    assert response.status_code == 422


def test_checkout_happy_path_creates_payment_and_returns_url(
    app: FastAPI, settings: Settings
) -> None:
    user = make_user()
    stripe_service = FakeStripeService(enabled=True)
    db_session = FakeDBSession()
    client = _client(app, user, stripe_service, db_session)

    response = client.post(
        "/billing/checkout",
        json={"pack_id": "pack_10"},
        headers={"Authorization": f"Bearer {_token(settings, user)}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["checkout_url"] == "https://checkout.stripe.com/c/pay/test_session"
    assert body["session_id"].startswith("cs_test_")
    assert body["amount_cents"] == 1_000
    assert body["credits"] == 10
    assert body["microcredits"] == 10_000_000

    assert len(db_session.payments) == 1
    payment = db_session.payments[0]
    assert payment.user_id == user.id
    assert payment.amount_cents == 1_000
    assert payment.credits_granted == 0
    assert payment.status == "pending"
    assert payment.stripe_session_id is not None
    assert payment.stripe_session_id.startswith("cs_test_")
    assert db_session.committed

    assert len(stripe_service.checkout_calls) == 1
    call = stripe_service.checkout_calls[0]
    assert call["user_id"] == user.id
    assert call["payment_id"] == payment.id
    assert call["pack"].id == "pack_10"


def test_checkout_stripe_failure_marks_payment_failed(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    stripe_service = FakeStripeService(enabled=True, fail=True)
    db_session = FakeDBSession()
    client = _client(app, user, stripe_service, db_session)

    response = client.post(
        "/billing/checkout",
        json={"pack_id": "pack_50"},
        headers={"Authorization": f"Bearer {_token(settings, user)}"},
    )
    # The route catches Stripe failures, marks the payment failed, commits, then
    # raises UpstreamError (502).
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"

    assert len(db_session.payments) == 1
    payment = db_session.payments[0]
    assert payment.status == "failed"
    assert payment.stripe_session_id is None
    assert db_session.committed


def test_checkout_with_custom_redirect_urls(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    stripe_service = FakeStripeService(enabled=True)
    db_session = FakeDBSession()
    client = _client(app, user, stripe_service, db_session)

    response = client.post(
        "/billing/checkout",
        json={
            "pack_id": "pack_50",
            "success_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel",
        },
        headers={"Authorization": f"Bearer {_token(settings, user)}"},
    )
    assert response.status_code == 200
    call = stripe_service.checkout_calls[0]
    assert call["success_url"] == "https://example.com/success"
    assert call["cancel_url"] == "https://example.com/cancel"


def test_checkout_rejects_non_https_redirect_urls(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    stripe_service = FakeStripeService(enabled=True)
    db_session = FakeDBSession()
    client = _client(app, user, stripe_service, db_session)

    response = client.post(
        "/billing/checkout",
        json={
            "pack_id": "pack_10",
            "success_url": "http://evil.com/success",
        },
        headers={"Authorization": f"Bearer {_token(settings, user)}"},
    )
    assert response.status_code == 422
