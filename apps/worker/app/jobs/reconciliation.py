"""Reconciliation job stub. Ledger comparison is out of scope for Phase 1."""

from typing import Any

from app.jobs.common import JobResult


async def reconciliation(ctx: dict[str, Any], *, run_id: str | None = None) -> JobResult:
    """Compare internal ledger state with provider billing records.

    Phase 1 only acknowledges the job. Later phases will run a read-only
    comparison and emit differences for review, never mutating balances from
    provider data directly.
    """
    del ctx, run_id
    # TODO(phase-2): snapshot balances, compare provider totals, emit discrepancies.
    return {"status": "noop", "job": "reconciliation"}
