"""Response contracts for the dashboard credit endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

__all__ = ["BalanceResponse", "LedgerEntryResponse", "LedgerListResponse"]


class BalanceResponse(BaseModel):
    """Wallet state in integer microcredits.

    ``balance_microcredits`` is the total unspent wallet (the ledger sum);
    ``available_microcredits = balance - reserved``.
    """

    balance_microcredits: int
    reserved_microcredits: int
    available_microcredits: int
    lifetime_purchased_microcredits: int
    lifetime_spent_microcredits: int


class LedgerEntryResponse(BaseModel):
    """A single append-only credit ledger entry."""

    id: str
    type: str
    amount_microcredits: int
    reference_id: str | None
    metadata: dict
    created_at: datetime


class LedgerListResponse(BaseModel):
    """A page of ledger entries, newest first."""

    items: list[LedgerEntryResponse]
    next_cursor: str | None = Field(default=None)
