"""Stripe webhook ingestion (Phase 5).

This endpoint is public (no auth). It verifies the Stripe signature, records
the event idempotently, and enqueues a worker job for credit grants. The API
never grants credits inline; the worker does that after fetching the event.

Outbox-lite ordering: the event row is committed *before* the job is enqueued,
so the worker can always find it. Replays of an unprocessed row are enqueued
again (the worker and ledger are idempotent); only a processed row is final.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.auth import request_settings
from app.core.errors import UpstreamError, WebhookSignatureError
from app.core.logging import get_logger
from app.db.session import get_db
from app.services.job_queue import PROCESS_WEBHOOK_JOB, JobQueue, get_job_queue
from app.services.stripe_service import construct_event
from app.services.webhook_service import SQLAlchemyWebhookEventStore, WebhookEventStore

router = APIRouter(tags=["webhooks"])

logger = get_logger(__name__)

# Delayed payment methods settle after checkout completion: Stripe sends an
# initial unpaid completion followed by async_payment_succeeded once paid.
ENQUEUE_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    }
)

__all__ = ["get_webhook_event_store", "router"]


def get_webhook_event_store(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookEventStore:
    """SQLAlchemy event store (overridable in tests)."""
    return SQLAlchemyWebhookEventStore(session)


async def _enqueue(
    store: WebhookEventStore,
    job_queue: JobQueue,
    *,
    event_row_id: Any,
    stripe_event_id: str,
    event_type: str,
) -> dict[str, str]:
    """Enqueue the worker job, recording a retryable failure if enqueue fails."""
    try:
        await job_queue.enqueue_job(PROCESS_WEBHOOK_JOB, event_id=str(event_row_id))
    except Exception:
        # Log only opaque identifiers: exception text from the queue/Stripe can
        # echo customer or card data. Leave processed_at NULL so the next Stripe
        # delivery re-enqueues this same row.
        logger.warning("webhook_enqueue_failed", event_id=stripe_event_id, event_type=event_type)
        try:
            event = await store.get_by_stripe_event_id(stripe_event_id)
            if event is not None:
                event.error = "enqueue failed"
                event.attempts = (event.attempts or 0) + 1
                await store.commit()
        except Exception:
            logger.warning(
                "webhook_enqueue_failure_record_failed",
                event_id=stripe_event_id,
                event_type=event_type,
            )
        raise
    return {"status": "queued"}


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(request_settings)],
    store: Annotated[WebhookEventStore, Depends(get_webhook_event_store)],
    job_queue: Annotated[JobQueue, Depends(get_job_queue)],
) -> dict[str, str]:
    """Verify, record, and enqueue a Stripe webhook.

    The raw body is read directly from the request (not parsed by FastAPI) so
    signature verification sees the exact bytes Stripe signed. Invalid
    signatures return 400. A new event is committed before enqueueing; a replay
    of an unprocessed event is enqueued again, while a processed replay is not.
    No credit ledger row is written here.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = settings.stripe_webhook_secret

    if not webhook_secret:
        raise WebhookSignatureError("Stripe webhook secret is not configured")

    event = construct_event(payload, sig_header, webhook_secret)
    stripe_event_id = str(getattr(event, "id", "") or "")
    event_type = str(getattr(event, "type", "") or "")
    if not stripe_event_id:
        raise WebhookSignatureError("Stripe event is missing an id")

    # The payload is persisted for the worker but never logged: it contains
    # customer data. Only the opaque event id and type are logged.
    event_data: dict[str, Any] = event.to_dict() if hasattr(event, "to_dict") else dict(event)
    event_row_id = await store.insert_if_new(
        stripe_event_id=stripe_event_id, event_type=event_type, payload=event_data
    )

    if event_row_id is None:
        existing = await store.get_by_stripe_event_id(stripe_event_id)
        if existing is None:
            # A conflict implies a committed row; without one we cannot safely
            # enqueue an opaque event id. Return 5xx so Stripe retries.
            logger.warning(
                "webhook_event_lookup_failed", event_id=stripe_event_id, event_type=event_type
            )
            raise UpstreamError("Webhook event could not be loaded")

        logger.info("webhook_replay", event_id=stripe_event_id, event_type=event_type)
        if existing.processed_at is not None:
            logger.info(
                "webhook_already_processed", event_id=stripe_event_id, event_type=event_type
            )
            return {"status": "already_processed"}
        # Unprocessed: a previous enqueue or worker attempt failed. Re-enqueue
        # the same row; both the worker and the ledger are idempotent.
        return await _enqueue(
            store,
            job_queue,
            event_row_id=existing.id,
            stripe_event_id=stripe_event_id,
            event_type=event_type,
        )

    logger.info("webhook_received", event_id=stripe_event_id, event_type=event_type)
    # Commit before enqueueing: the worker must never see a job for an event
    # whose row is still inside this request's uncommitted transaction.
    await store.commit()

    if event_type not in ENQUEUE_EVENT_TYPES:
        logger.info("webhook_ignored", event_id=stripe_event_id, event_type=event_type)
        return {"status": "ignored"}

    return await _enqueue(
        store,
        job_queue,
        event_row_id=event_row_id,
        stripe_event_id=stripe_event_id,
        event_type=event_type,
    )
