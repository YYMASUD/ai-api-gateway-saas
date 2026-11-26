"""Thin Stripe wrapper.

The ``stripe`` SDK is imported lazily inside each function so this module can be
imported (and the app can boot) when Stripe is unconfigured. Nothing here logs
the secret key, the signature header, or any card data.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.config import Settings, get_settings
from app.core.errors import ServiceUnavailableError, WebhookSignatureError
from app.services.credit_packs import Pack

__all__ = [
    "StripeService",
    "construct_event",
    "create_checkout_session",
    "is_enabled",
]


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def is_enabled(settings: Settings | None = None) -> bool:
    """Whether a Stripe secret key is configured."""
    return _settings(settings).stripe_enabled


def _price_id(settings: Settings, pack: Pack) -> str:
    """Resolve the configured Stripe price id for ``pack`` or raise 503."""
    price_id = getattr(settings, pack.price_setting, "")
    if not price_id:
        raise ServiceUnavailableError(
            f"Stripe price for {pack.id} is not configured", code="stripe_not_configured"
        )
    return price_id


def _require_secret(settings: Settings) -> str:
    if not settings.stripe_enabled:
        raise ServiceUnavailableError("Stripe is not configured", code="stripe_not_configured")
    return settings.stripe_secret_key


def create_checkout_session(
    *,
    user_id: uuid.UUID,
    payment_id: uuid.UUID,
    pack: Pack,
    success_url: str,
    cancel_url: str,
    settings: Settings | None = None,
) -> Any:
    """Create a Stripe Checkout session for a single credit pack.

    The session carries exactly one line item referencing the pack's configured
    price id, and records the buyer, payment, pack, and credit amount in
    ``client_reference_id`` and ``metadata`` so the worker can settle the
    payment without trusting the client.
    """
    resolved = _settings(settings)
    secret = _require_secret(resolved)
    price_id = _price_id(resolved, pack)

    import stripe  # local import: keeps the app importable without the SDK wired up

    stripe.api_key = secret
    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user_id),
        metadata={
            "user_id": str(user_id),
            "payment_id": str(payment_id),
            "pack_id": pack.id,
            "credits": str(pack.credits),
        },
    )


def construct_event(payload: bytes, sig_header: str, secret: str) -> Any:
    """Verify a Stripe webhook signature and return the parsed event.

    A ``stripe.SignatureVerificationError`` is translated into a
    ``WebhookSignatureError`` (400, ``invalid_signature``) so the caller never
    leaks SDK details. The signature header and parsed payload are never logged.
    """
    import stripe

    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    except stripe.SignatureVerificationError as exc:
        raise WebhookSignatureError("Invalid Stripe webhook signature") from exc


class StripeService:
    """Instance wrapper bound to a settings object; mirrors the module functions."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = _settings(settings)

    @property
    def settings(self) -> Settings:
        return self._settings

    def is_enabled(self) -> bool:
        return is_enabled(self._settings)

    def create_checkout_session(
        self,
        *,
        user_id: uuid.UUID,
        payment_id: uuid.UUID,
        pack: Pack,
        success_url: str,
        cancel_url: str,
    ) -> Any:
        return create_checkout_session(
            user_id=user_id,
            payment_id=payment_id,
            pack=pack,
            success_url=success_url,
            cancel_url=cancel_url,
            settings=self._settings,
        )

    def construct_event(self, payload: bytes, sig_header: str, secret: str) -> Any:
        return construct_event(payload, sig_header, secret)
