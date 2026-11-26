"""Dashboard credit balance and ledger endpoints (JWT protected, owner-only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.credits import BalanceResponse, LedgerEntryResponse, LedgerListResponse
from app.services.credit_service import (
    CreditRepository,
    CreditService,
    SQLAlchemyCreditRepository,
)

router = APIRouter(tags=["credits"])

DEFAULT_LEDGER_LIMIT = 50
MAX_LEDGER_LIMIT = 100


def get_credit_repository(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CreditRepository:
    """SQLAlchemy-backed credit persistence (overridable in tests)."""
    return SQLAlchemyCreditRepository(session)


def get_credit_service(
    repository: Annotated[CreditRepository, Depends(get_credit_repository)],
) -> CreditService:
    return CreditService(repository)


@router.get("", response_model=BalanceResponse)
async def get_credit_balance(
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[CreditService, Depends(get_credit_service)],
) -> BalanceResponse:
    """Return the authenticated user's wallet state."""
    balance = await service.get_balance(None, user_id=user.id)
    return BalanceResponse(
        balance_microcredits=balance.balance,
        reserved_microcredits=balance.reserved,
        available_microcredits=balance.balance - balance.reserved,
        lifetime_purchased_microcredits=balance.lifetime_purchased_microcredits,
        lifetime_spent_microcredits=balance.lifetime_spent_microcredits,
    )


@router.get("/ledger", response_model=LedgerListResponse)
async def list_credit_ledger(
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[CreditService, Depends(get_credit_service)],
    limit: Annotated[int, Query(ge=1, le=MAX_LEDGER_LIMIT)] = DEFAULT_LEDGER_LIMIT,
    cursor: str | None = None,
) -> LedgerListResponse:
    """Return a page of the authenticated user's ledger, newest first."""
    entries = await service.list_ledger(None, user_id=user.id, limit=limit, cursor=cursor)
    items = [
        LedgerEntryResponse(
            id=str(entry.id),
            type=entry.type,
            amount_microcredits=entry.amount_microcredits,
            reference_id=entry.reference_id,
            metadata=dict(entry.metadata_json or {}),
            created_at=entry.created_at,
        )
        for entry in entries
    ]
    next_cursor = str(entries[-1].id) if len(entries) == limit and entries else None
    return LedgerListResponse(items=items, next_cursor=next_cursor)
