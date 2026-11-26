"""OpenAI-compatible model listing for the ``/v1`` surface.

Resolves ENABLED ``model_pricing`` rows and renders them in OpenAI's
``{"object": "list", "data": [...]}`` envelope. Listing models is free: no
credit reservation is performed.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.auth import get_api_key_principal, require_scope
from app.services.api_key_service import APIKeyPrincipal
from app.services.model_pricing import (
    ModelPricingRepository,
    get_model_pricing_repository,
)

router = APIRouter(tags=["models"])


@router.get("/models")
async def list_models(
    principal: Annotated[APIKeyPrincipal, Depends(get_api_key_principal)],
    repository: Annotated[ModelPricingRepository, Depends(get_model_pricing_repository)],
) -> dict[str, Any]:
    """Return enabled ``model_pricing`` rows in the OpenAI list-models shape."""
    require_scope(principal, "models:read")
    rows = await repository.list_enabled()
    return {
        "object": "list",
        "data": [
            {
                "id": row.model_id,
                "object": "model",
                "created": int(row.created_at.timestamp()),
                "owned_by": row.provider,
            }
            for row in rows
        ],
    }
