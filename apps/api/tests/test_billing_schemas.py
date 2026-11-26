"""Billing request/response schema validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.billing import CheckoutRequest, CheckoutResponse
from app.services.credit_packs import PACKS


def test_checkout_request_accepts_known_pack() -> None:
    body = CheckoutRequest(pack_id="pack_10")
    assert body.pack_id == "pack_10"
    assert body.success_url is None
    assert body.cancel_url is None


def test_checkout_request_rejects_unknown_pack() -> None:
    with pytest.raises(ValidationError):
        CheckoutRequest(pack_id="pack_999")


def test_checkout_request_accepts_https_redirect_urls() -> None:
    body = CheckoutRequest(
        pack_id="pack_50",
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
    )
    assert body.success_url == "https://example.com/success"
    assert body.cancel_url == "https://example.com/cancel"


def test_checkout_request_accepts_localhost_http_urls() -> None:
    body = CheckoutRequest(
        pack_id="pack_10",
        success_url="http://localhost:3000/dashboard/billing?checkout=success",
        cancel_url="http://127.0.0.1:3000/dashboard/billing?checkout=cancelled",
    )
    assert body.success_url is not None
    assert body.cancel_url is not None


def test_checkout_request_rejects_non_https_non_local() -> None:
    with pytest.raises(ValidationError):
        CheckoutRequest(
            pack_id="pack_10",
            success_url="http://evil.com/steal",
        )


def test_checkout_response_from_pack_has_exact_integers() -> None:
    pack = PACKS["pack_100"]
    response = CheckoutResponse.from_pack(
        pack=pack,
        session_id="cs_test_123",
        checkout_url="https://checkout.stripe.com/c/pay/123",
    )
    assert response.amount_cents == 10_000
    assert response.credits == 100
    assert response.microcredits == 100_000_000
    assert isinstance(response.amount_cents, int)
    assert isinstance(response.credits, int)
    assert isinstance(response.microcredits, int)
