"""Stripe wrapper tests. All Stripe SDK calls are mocked; no network."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import stripe

from app.config import Settings
from app.core.errors import ServiceUnavailableError, WebhookSignatureError
from app.services.credit_packs import PACKS
from app.services.stripe_service import (
    StripeService,
    construct_event,
    create_checkout_session,
    is_enabled,
)


def _configure(settings: Settings) -> Settings:
    settings.stripe_secret_key = "sk_test_fake_not_a_secret"
    settings.stripe_price_10 = "price_10_fake"
    settings.stripe_price_50 = "price_50_fake"
    settings.stripe_price_100 = "price_100_fake"
    return settings


def test_module_import_does_not_require_configured_sdk() -> None:
    """The wrapper module imports with blank Stripe settings."""
    from app.services import stripe_service

    assert hasattr(stripe_service, "create_checkout_session")
    assert hasattr(stripe_service, "construct_event")


def test_is_enabled_follows_secret_key(settings: Settings) -> None:
    assert is_enabled(settings) is False
    _configure(settings)
    assert is_enabled(settings) is True


def test_create_checkout_session_builds_exact_request(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    resolved = _configure(settings)
    captured: dict[str, Any] = {}

    def _fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(id="cs_test_123", url="https://checkout.stripe.com/c/pay/123")

    monkeypatch.setattr(stripe.checkout.Session, "create", _fake_create)
    monkeypatch.setattr(stripe, "api_key", "")
    user_id = uuid.uuid4()
    payment_id = uuid.uuid4()

    result = create_checkout_session(
        user_id=user_id,
        payment_id=payment_id,
        pack=PACKS["pack_10"],
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
        settings=resolved,
    )

    assert result.id == "cs_test_123"
    assert stripe.api_key == "sk_test_fake_not_a_secret"
    assert captured["mode"] == "payment"
    assert captured["line_items"] == [{"price": "price_10_fake", "quantity": 1}]
    assert captured["client_reference_id"] == str(user_id)
    assert captured["success_url"] == "https://example.com/success"
    assert captured["cancel_url"] == "https://example.com/cancel"
    assert captured["metadata"] == {
        "user_id": str(user_id),
        "payment_id": str(payment_id),
        "pack_id": "pack_10",
        "credits": "10",
    }
    # Neither the secret key nor any card data may reach logs.
    assert "sk_test_fake_not_a_secret" not in caplog.text


def test_create_checkout_session_unconfigured_raises_503(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kwargs: None)
    monkeypatch.setattr(stripe, "api_key", "")

    with pytest.raises(ServiceUnavailableError) as exc:
        create_checkout_session(
            user_id=uuid.uuid4(),
            payment_id=uuid.uuid4(),
            pack=PACKS["pack_10"],
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
            settings=settings,
        )
    assert exc.value.status_code == 503
    # api_key is set only when enabled.
    assert stripe.api_key == ""


def test_create_checkout_session_missing_price_raises_503(
    settings: Settings,
) -> None:
    settings.stripe_secret_key = "sk_test_fake_not_a_secret"
    settings.stripe_price_10 = ""

    with pytest.raises(ServiceUnavailableError) as exc:
        create_checkout_session(
            user_id=uuid.uuid4(),
            payment_id=uuid.uuid4(),
            pack=PACKS["pack_10"],
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
            settings=settings,
        )
    assert exc.value.status_code == 503


def test_construct_event_returns_verified_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"id": "evt_123", "type": "checkout.session.completed"}
    captured: dict[str, Any] = {}

    def _fake_construct(payload: bytes, sig: str, secret: str) -> Any:
        captured.update(payload=payload, sig=sig, secret=secret)
        return expected

    monkeypatch.setattr(stripe.Webhook, "construct_event", _fake_construct)
    payload = b'{"id":"evt_123"}'
    result = construct_event(payload, "t=1,v1=abc", "whsec_fake")

    assert result is expected
    assert captured["payload"] == payload
    assert captured["sig"] == "t=1,v1=abc"
    assert captured["secret"] == "whsec_fake"


def test_construct_event_translates_signature_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_construct(payload: bytes, sig: str, secret: str) -> Any:
        raise stripe.SignatureVerificationError("bad signature", "t=1,v1=bad")

    monkeypatch.setattr(stripe.Webhook, "construct_event", _fake_construct)

    with pytest.raises(WebhookSignatureError) as exc:
        construct_event(b"{}", "t=1,v1=bad", "whsec_fake")
    assert exc.value.status_code == 400
    assert exc.value.code == "invalid_signature"


def test_stripe_service_instance_delegates(settings: Settings) -> None:
    resolved = _configure(settings)
    service = StripeService(resolved)
    assert service.is_enabled() is True
    assert service.settings is resolved
