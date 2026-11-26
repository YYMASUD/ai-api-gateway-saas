"""Stripe Checkout endpoints (JWT protected, owner-only)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.core.auth import get_current_user, request_settings
from app.core.errors import ServiceUnavailableError, UpstreamError
from app.core.logging import get_logger
from app.db.models import Payment, User
from app.db.session import get_db
from app.schemas.billing import CheckoutRequest, CheckoutResponse
from app.services.credit_packs import get_pack
from app.services.stripe_service import StripeService

router = APIRouter(tags=["billing"])

logger = get_logger(__name__)

__all__ = ["get_stripe_service", "router"]


def get_stripe_service(request: Request) -> StripeService:
    """Stripe wrapper bound to this app's settings (overridable in tests)."""
    settings: Settings = request_settings(request)
    return StripeService(settings)


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout_session(
    body: CheckoutRequest,
    db_session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(request_settings)],
    stripe_service: Annotated[StripeService, Depends(get_stripe_service)],
) -> CheckoutResponse:
    """Start a Checkout session for a credit pack.

    A pending ``Payment`` is created first so the worker can always reconcile a
    checkout, then Stripe is called, then the session id is stored. Stripe API
    failures mark that row ``failed`` and are surfaced as 502; no credits are
    granted here — the verified webhook enqueues the worker's grant job.
    """
    pack = get_pack(body.pack_id)
    if pack is None:  # defensive: the schema validator normally rejects this
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="unknown pack_id")

    if not stripe_service.is_enabled():
        raise ServiceUnavailableError("Stripe is not configured", code="stripe_not_configured")

    payment = Payment(
        id=uuid.uuid4(),
        user_id=user.id,
        amount_cents=pack.amount_cents,
        credits_granted=0,
        status="pending",
        currency="usd",
        metadata_json={"pack_id": pack.id, "source": "checkout"},
    )
    db_session.add(payment)

    try:
        # The Stripe SDK is synchronous: offload it so the event loop keeps
        # serving other requests while the upstream HTTP call is in flight.
        checkout_session = await run_in_threadpool(
            stripe_service.create_checkout_session,
            user_id=user.id,
            payment_id=payment.id,
            pack=pack,
            success_url=body.success_url or settings.stripe_success_url,
            cancel_url=body.cancel_url or settings.stripe_cancel_url,
        )
        session_id = str(getattr(checkout_session, "id", "") or "")
        checkout_url = str(getattr(checkout_session, "url", "") or "")
        if not session_id or not checkout_url:
            raise UpstreamError("Stripe returned an incomplete checkout session")
    except ServiceUnavailableError:
        payment.status = "failed"
        await db_session.commit()
        raise
    except Exception as exc:
        payment.status = "failed"
        await db_session.commit()
        # Log only the exception type and the opaque payment id: Stripe errors
        # can echo request details, and card data must never reach the logs.
        logger.warning(
            "checkout_session_failed",
            payment_id=str(payment.id),
            error_type=type(exc).__name__,
        )
        raise UpstreamError("Could not create Stripe checkout session") from exc

    payment.stripe_session_id = session_id
    await db_session.commit()
    return CheckoutResponse.from_pack(pack=pack, session_id=session_id, checkout_url=checkout_url)
