"""Webhook processing job stub. Provider verification arrives in a later phase."""

from typing import Any

from app.jobs.common import JobResult


async def process_webhook(ctx: dict[str, Any], *, event_id: str) -> JobResult:
    """Process a provider webhook by opaque event identifier.

    Phase 1 only acknowledges the job. Later phases will verify signatures,
    deduplicate events, and update billing state. The webhook body is never
    logged by this worker; pass only the ID needed to fetch it from storage.
    """
    del ctx, event_id
    # TODO(phase-2): verify the provider signature and apply the idempotent update.
    return {"status": "noop", "job": "process_webhook"}
