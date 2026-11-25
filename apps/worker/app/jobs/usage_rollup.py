"""Usage rollup job stub. Aggregates are deferred to a later phase."""

from typing import Any

from app.jobs.common import JobResult


async def usage_rollup(
    ctx: dict[str, Any],
    *,
    window_start: str,
    window_end: str,
) -> JobResult:
    """Roll usage into the requested UTC window.

    ``window_start`` and ``window_end`` are ISO 8601 timestamps supplied by the
    scheduler. Phase 1 only acknowledges the job; later phases will aggregate
    token counts and cost by tenant without logging prompts or model inputs.
    """
    del ctx, window_start, window_end
    # TODO(phase-2): aggregate usage rows into tenant/model reporting buckets.
    return {"status": "noop", "job": "usage_rollup"}
