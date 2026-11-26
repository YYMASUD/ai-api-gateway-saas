"""Idempotent Stripe webhook settlement.

FastAPI verifies the Stripe signature and stores the raw event; this worker is
the *only* place where a purchase grants credits. The caller passes just the
local webhook-event UUID, so no payload, customer e-mail, or card data is ever
logged here -- logs carry only the event id, its type, and the outcome.

Credits are granted only for a *paid*, USD checkout whose ``amount_total``
matches a catalog pack, using the catalog's integer cent -> microcredit
conversion. A client-supplied ``metadata.credits`` value is cross-checked
against the catalog and never trusted as the grant source.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from app.db.models import Payment, WebhookEvent  # type: ignore[import-untyped]
from app.services.credit_packs import (  # type: ignore[import-untyped]
    MAX_CENTS,
    microcredits_for_cents,
    pack_for_cents,
)
from app.services.credit_service import CreditService  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from worker_app.db import session_scope

logger = structlog.get_logger(__name__)

CHECKOUT_SESSION_COMPLETED = "checkout.session.completed"
CHECKOUT_SESSION_ASYNC_PAYMENT_SUCCEEDED = "checkout.session.async_payment_succeeded"
SETTLEABLE_EVENT_TYPES = frozenset(
    {CHECKOUT_SESSION_COMPLETED, CHECKOUT_SESSION_ASYNC_PAYMENT_SUCCEEDED}
)
MAX_SESSION_ID_LENGTH = 128
USD = "usd"


class WebhookProcessingError(Exception):
    """A validation failure carrying a short, payload-free reason for the row."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Checkout:
    """Server-authoritative fields extracted from a verified event payload."""

    session_id: str
    payment_intent: str | None
    amount_cents: int
    user_id: uuid.UUID
    payment_id: uuid.UUID | None


def _log(event_id: str, event_type: str, outcome: str) -> None:
    """Log only the event id, its type, and the outcome -- never the payload."""
    logger.info("webhook", event_id=event_id, type=event_type, outcome=outcome)


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    """Return a stripped non-empty string, rejecting non-string JSON values."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalized(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_credits(raw_credits: object) -> int | None:
    """Parse ``metadata.credits`` strictly: whole-credit string or integer only."""
    if isinstance(raw_credits, bool):
        return None
    if isinstance(raw_credits, int):
        return raw_credits
    if isinstance(raw_credits, str):
        text = raw_credits.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def _parse_checkout(payload: Mapping[str, Any]) -> _Checkout:
    """Validate a paid, USD, catalog-priced checkout and extract its grant data."""
    session_object = _mapping(_mapping(payload.get("data")).get("object"))

    session_id = _text(session_object.get("id"))
    if session_id is None:
        raise WebhookProcessingError("missing session id")
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        raise WebhookProcessingError("session id too long")

    if _normalized(session_object.get("payment_status")) != "paid":
        raise WebhookProcessingError("payment not paid")

    if _normalized(session_object.get("currency")) != USD:
        raise WebhookProcessingError("invalid currency")

    amount_cents = session_object.get("amount_total")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        raise WebhookProcessingError("invalid amount_total")
    if amount_cents > MAX_CENTS:
        raise WebhookProcessingError("amount_total exceeds maximum")
    pack = pack_for_cents(amount_cents)
    if pack is None:
        raise WebhookProcessingError("amount_total not in catalog")

    metadata = _mapping(session_object.get("metadata"))

    if "credits" not in metadata:
        raise WebhookProcessingError("missing metadata credits")
    metadata_credits = _parse_credits(metadata.get("credits"))
    if metadata_credits is None or metadata_credits != pack.credits:
        raise WebhookProcessingError("metadata credits mismatch")

    if "pack_id" in metadata:
        metadata_pack = metadata.get("pack_id")
        if not isinstance(metadata_pack, str) or metadata_pack.strip() != pack.id:
            raise WebhookProcessingError("metadata pack id mismatch")

    client_user = _text(session_object.get("client_reference_id"))
    metadata_user = _text(metadata.get("user_id"))
    if client_user is not None and metadata_user is not None and client_user != metadata_user:
        raise WebhookProcessingError("user id mismatch")
    user_text = client_user or metadata_user
    if user_text is None:
        raise WebhookProcessingError("missing user id")
    user_id = _parse_uuid(user_text)
    if user_id is None:
        raise WebhookProcessingError("invalid user id")

    payment_id: uuid.UUID | None = None
    metadata_payment = _text(metadata.get("payment_id"))
    if metadata_payment is not None:
        payment_id = _parse_uuid(metadata_payment)

    return _Checkout(
        session_id=session_id,
        payment_intent=_text(session_object.get("payment_intent")),
        amount_cents=amount_cents,
        user_id=user_id,
        payment_id=payment_id,
    )


async def _load_event(session: AsyncSession, event_id: str) -> WebhookEvent | None:
    """Fetch the event row, treating a malformed UUID as a missing event."""
    parsed = _parse_uuid(event_id)
    if parsed is None:
        return None
    result = await session.execute(select(WebhookEvent).where(WebhookEvent.id == parsed))
    return result.scalar_one_or_none()


async def _find_payment(session: AsyncSession, checkout: _Checkout) -> Payment | None:
    """Resolve the payment row by Stripe session id, else by metadata payment id."""
    by_session = await session.execute(
        select(Payment).where(Payment.stripe_session_id == checkout.session_id)
    )
    payment = by_session.scalar_one_or_none()
    if payment is not None or checkout.payment_id is None:
        return payment
    by_id = await session.execute(select(Payment).where(Payment.id == checkout.payment_id))
    return by_id.scalar_one_or_none()


def _validate_payment(payment: Payment, checkout: _Checkout) -> None:
    """Refuse to settle a payment that is not this buyer's matching USD charge."""
    if payment.user_id != checkout.user_id:
        raise WebhookProcessingError("payment user mismatch")
    if payment.amount_cents != checkout.amount_cents:
        raise WebhookProcessingError("payment amount mismatch")
    if payment.stripe_session_id not in (None, checkout.session_id):
        raise WebhookProcessingError("payment session mismatch")
    if _normalized(payment.currency) != USD:
        raise WebhookProcessingError("payment currency mismatch")


async def _check_payment_intent_available(
    session: AsyncSession, payment: Payment, payment_intent: str | None
) -> None:
    """Reject an intent owned by another payment before the unique index fires."""
    if payment_intent is None or payment.stripe_payment_intent == payment_intent:
        return
    result = await session.execute(
        select(Payment).where(Payment.stripe_payment_intent == payment_intent)
    )
    other = result.scalar_one_or_none()
    if other is not None and other.id != payment.id:
        raise WebhookProcessingError("payment intent collision")


async def _record_failure(event_id: str, exc: Exception) -> None:
    """Commit a short error and a bumped attempt counter in a fresh transaction.

    ``processed_at`` stays NULL so ARQ retries the event. A failure to persist
    this bookkeeping must never mask the original exception the caller re-raises.
    """
    reason = exc.reason if isinstance(exc, WebhookProcessingError) else "processing failed"
    try:
        async with session_scope() as session:
            event = await _load_event(session, event_id)
            if event is None:
                return
            event.error = reason
            event.attempts = (event.attempts or 0) + 1
            logger.warning(
                "webhook.failed", event_id=event_id, type=event.type or "-", outcome="failed"
            )
    except Exception:
        logger.warning("webhook.failed", event_id=event_id, outcome="error_record_failed")


async def _settle(session: AsyncSession, event: WebhookEvent, event_id: str) -> dict[str, Any]:
    """Grant, update the payment, and mark the event processed in one transaction."""
    checkout = _parse_checkout(cast(Mapping[str, Any], event.payload))
    payment = await _find_payment(session, checkout)
    if payment is None:
        raise WebhookProcessingError("payment not found")

    _validate_payment(payment, checkout)
    await _check_payment_intent_available(session, payment, checkout.payment_intent)

    amount_microcredits = microcredits_for_cents(checkout.amount_cents)
    await CreditService(session).grant_purchase(
        session,
        user_id=checkout.user_id,
        amount_microcredits=amount_microcredits,
        reference_id=checkout.session_id,
        idempotency_key=f"stripe:{checkout.session_id}",
    )

    payment.status = "completed"
    if checkout.payment_intent is not None:
        payment.stripe_payment_intent = checkout.payment_intent
    payment.amount_cents = checkout.amount_cents
    payment.credits_granted = amount_microcredits

    event.processed_at = datetime.now(UTC)
    event.error = None

    _log(event_id, event.type, "processed")
    return {
        "status": "processed",
        "type": event.type,
        "event_id": event_id,
        "amount_microcredits": amount_microcredits,
    }


async def process_webhook(ctx: dict[str, Any], *, event_id: str) -> dict[str, Any]:
    """Apply a verified webhook event exactly once and grant its purchase credits.

    Returns ``processed`` on a fresh paid checkout, ``already_processed`` on
    replay (no second grant), ``skipped`` when the event row is gone, and
    ``ignored`` for non-settleable event types (marked processed). Validation or
    persistence failures record an error, bump ``attempts``, and re-raise so ARQ
    retries the job.
    """
    del ctx  # ARQ context is unused; the job only needs the opaque event id.
    try:
        async with session_scope() as session:
            event = await _load_event(session, event_id)
            if event is None:
                _log(event_id, "-", "skipped")
                return {"status": "skipped", "reason": "event_not_found"}

            if event.processed_at is not None:
                _log(event_id, event.type or "-", "already_processed")
                return {"status": "already_processed"}

            if event.type not in SETTLEABLE_EVENT_TYPES:
                event.processed_at = datetime.now(UTC)
                event.error = None
                _log(event_id, event.type, "ignored")
                return {"status": "ignored", "type": event.type}

            return await _settle(session, event, event_id)
    except Exception as exc:
        await _record_failure(event_id, exc)
        raise


__all__ = [
    "CHECKOUT_SESSION_ASYNC_PAYMENT_SUCCEEDED",
    "CHECKOUT_SESSION_COMPLETED",
    "MAX_SESSION_ID_LENGTH",
    "SETTLEABLE_EVENT_TYPES",
    "WebhookProcessingError",
    "process_webhook",
]
