"""FastAPI auth dependencies: dashboard JWTs and (future /v1) API keys."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import ACCESS_TOKEN_TYPE, TokenVerificationError, verify_jwt
from app.db.models import User
from app.db.session import get_db
from app.services.api_key_service import (
    APIKeyPrincipal,
    APIKeyRepository,
    APIKeyService,
    SQLAlchemyAPIKeyRepository,
)

_bearer = HTTPBearer(auto_error=False)


def request_settings(request: Request) -> Settings:
    """Resolve the app's settings, falling back to the env-backed singleton."""
    resolved: Settings | None = getattr(request.app.state, "settings", None)
    return resolved if resolved is not None else get_settings()


def _invalid_token() -> AuthenticationError:
    return AuthenticationError("Invalid or expired authentication token", code="invalid_token")


def verify_access_token_claims(claims: dict[str, Any]) -> uuid.UUID:
    """Validate the ``type`` and ``sub`` claims and return the user UUID."""
    if claims.get("type") != ACCESS_TOKEN_TYPE:
        raise TokenVerificationError("invalid token type")
    subject = claims.get("sub")
    if not subject:
        raise TokenVerificationError("missing subject")
    try:
        return uuid.UUID(str(subject))
    except (ValueError, TypeError) as exc:
        raise TokenVerificationError("invalid subject") from exc


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the active user from a Bearer JWT, or raise 401/403."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _invalid_token()
    settings = request_settings(request)
    try:
        claims = verify_jwt(
            credentials.credentials,
            settings.jwt_secret,
            settings.jwt_issuer,
            settings.jwt_audience,
        )
        user_id = verify_access_token_claims(claims)
    except TokenVerificationError:
        raise _invalid_token() from None
    user = await session.get(User, user_id)
    if user is None:
        raise _invalid_token()
    if user.status == "suspended":
        raise PermissionDeniedError("Account is suspended", code="user_suspended")
    if user.status != "active":
        raise _invalid_token()
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require an authenticated administrator for sensitive routes."""
    if user.role != "admin":
        raise PermissionDeniedError("Administrator access required", code="forbidden")
    return user


def get_api_key_repository(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> APIKeyRepository:
    """SQLAlchemy-backed API-key persistence (overridable in tests)."""
    return SQLAlchemyAPIKeyRepository(session)


async def get_api_key_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    repository: Annotated[APIKeyRepository, Depends(get_api_key_repository)],
) -> APIKeyPrincipal:
    """Bearer API-key dependency for the future ``/v1`` surface.

    No public diagnostic route is registered; tests exercise this dependency
    via a repository override or call the service directly.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Invalid API key", code="invalid_api_key")
    redis_client = getattr(request.app.state, "redis", None)
    service = APIKeyService(repository, request_settings(request), redis_client)
    return await service.authenticate(credentials.credentials)


def require_scope(principal: APIKeyPrincipal, scope: str) -> None:
    """Raise ``PermissionDeniedError`` (403) if the principal lacks ``scope``.

    API keys carry an explicit scope list; the gateway's ``/v1`` routes
    consult this helper to refuse calls the key was not authorised for.
    The error code ``insufficient_scope`` matches OpenAI's documented
    permission-error vocabulary.
    """
    if scope not in principal.scopes:
        raise PermissionDeniedError(
            f"API key is missing required scope: {scope}",
            code="insufficient_scope",
        )


__all__ = [
    "get_api_key_principal",
    "get_api_key_repository",
    "get_current_user",
    "request_settings",
    "require_admin",
    "require_scope",
    "verify_access_token_claims",
]
