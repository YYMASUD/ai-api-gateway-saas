"""Dashboard API-key lifecycle endpoints (JWT protected)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from redis.asyncio import Redis

from app.core.auth import get_api_key_repository, get_current_user, request_settings
from app.db.models import APIKey, User
from app.schemas.auth import (
    APIKeyCreateRequest,
    APIKeyCreateResponse,
    APIKeyResponse,
)
from app.services.api_key_service import (
    APIKeyRepository,
    APIKeyService,
    client_ip,
    display_prefix,
)

router = APIRouter(tags=["dashboard"])


def get_api_key_service(
    request: Request,
    repository: Annotated[APIKeyRepository, Depends(get_api_key_repository)],
) -> APIKeyService:
    redis: Redis | None = getattr(request.app.state, "redis", None)
    return APIKeyService(repository, request_settings(request), redis)


def _response(key: APIKey) -> APIKeyResponse:
    return APIKeyResponse(
        id=str(key.id),
        name=key.name,
        prefix=display_prefix(key.prefix),
        scopes=list(key.scopes),
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        created_at=key.created_at,
    )


@router.post(
    "/keys",
    response_model=APIKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_key(
    payload: APIKeyCreateRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[APIKeyService, Depends(get_api_key_service)],
) -> APIKeyCreateResponse:
    """Create a key and return the full secret exactly once."""
    key, raw_key = await service.create_key(
        user_id=user.id,
        name=payload.name,
        scopes=list(payload.scopes),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return APIKeyCreateResponse(
        id=str(key.id),
        name=key.name,
        prefix=display_prefix(key.prefix),
        key=raw_key,
        scopes=list(key.scopes),
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        created_at=key.created_at,
    )


@router.get("/keys", response_model=list[APIKeyResponse])
async def list_keys(
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[APIKeyService, Depends(get_api_key_service)],
) -> list[APIKeyResponse]:
    """List key metadata; hashes and plaintext keys are never returned."""
    keys = await service.list_keys(user.id)
    return [_response(key) for key in keys]


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    service: Annotated[APIKeyService, Depends(get_api_key_service)],
) -> Response:
    """Revoke an owned key, purge its cache entry, and audit the action."""
    await service.revoke_key(
        user_id=user.id,
        key_id=key_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
