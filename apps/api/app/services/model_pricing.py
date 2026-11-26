"""Model-pricing persistence for the ``/v1`` surface.

Kept separate from the integer-only ``pricing`` arithmetic so the routes can
depend on a narrow, overridable repository instead of a raw ORM session.
Only ENABLED rows are ever exposed or routed.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ModelPricing
from app.db.session import get_db

__all__ = [
    "ModelPricingRepository",
    "SQLAlchemyModelPricingRepository",
    "get_model_pricing_repository",
]


class ModelPricingRepository(Protocol):
    """Read-only access to enabled model-pricing rows."""

    async def list_enabled(self) -> list[ModelPricing]: ...

    async def get_enabled(self, model_id: str) -> ModelPricing | None: ...


class SQLAlchemyModelPricingRepository:
    """SQLAlchemy-backed pricing repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_enabled(self) -> list[ModelPricing]:
        result = await self._session.execute(
            select(ModelPricing)
            .where(ModelPricing.enabled.is_(True))
            .order_by(ModelPricing.model_id.asc())
        )
        return list(result.scalars().all())

    async def get_enabled(self, model_id: str) -> ModelPricing | None:
        result = await self._session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.enabled.is_(True),
            )
        )
        return result.scalar_one_or_none()


def get_model_pricing_repository(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ModelPricingRepository:
    """SQLAlchemy-backed pricing persistence (overridable in tests)."""
    return SQLAlchemyModelPricingRepository(session)
