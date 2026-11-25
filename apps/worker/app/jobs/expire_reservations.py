"""Reservation expiry job stub. Reservation lifecycle is implemented later."""

from typing import Any

from app.jobs.common import JobResult


async def expire_reservations(ctx: dict[str, Any], *, batch_size: int = 100) -> JobResult:
    """Expire stale credit reservations in bounded batches.

    Phase 1 only acknowledges the job. Later phases will release reserved funds
    in a transaction after confirming the reservation has expired, using
    ``batch_size`` to cap work per invocation.
    """
    del ctx, batch_size
    # TODO(phase-2): release expired reservations and write audit records.
    return {"status": "noop", "job": "expire_reservations"}
