"""Settlement job stub. Real credit accounting belongs to a later phase."""

from typing import Any

from app.jobs.common import JobResult


async def settle_credits(ctx: dict[str, Any], *, usage_event_id: str) -> JobResult:
    """Settle a recorded usage event.

    Phase 1 only acknowledges the job. Later phases will atomically move credits
    from reservation to charge and write an audit record. Never log prompts,
    provider keys, or raw request/response bodies from this context.
    """
    del ctx, usage_event_id
    # TODO(phase-2): load the usage event and settle the reserved credit amount.
    return {"status": "noop", "job": "settle_credits"}
