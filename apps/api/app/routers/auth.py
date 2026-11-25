"""Browser-facing email/password authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, request_settings
from app.db.models import User
from app.db.session import get_db
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import (
    AuthRepository,
    AuthService,
    SQLAlchemyAuthRepository,
)

router = APIRouter(tags=["auth"])


def get_auth_repository(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AuthRepository:
    """SQLAlchemy-backed user persistence (overridable in tests)."""
    return SQLAlchemyAuthRepository(session)


def get_auth_service(
    request: Request,
    repository: Annotated[AuthRepository, Depends(get_auth_repository)],
) -> AuthService:
    return AuthService(repository, request_settings(request))


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
    )


def _token_response(user: User, service: AuthService) -> TokenResponse:
    token, expires_in = service.issue_token(user)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user=_user_response(user),
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    """Create a local account and return an access token."""
    user = await service.register(
        email=str(payload.email),
        password=payload.password,
        display_name=payload.display_name,
    )
    return _token_response(user, service)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    """Authenticate and issue a short-lived access token."""
    user = await service.login(email=str(payload.email), password=payload.password)
    return _token_response(user, service)


@router.get("/me", response_model=UserResponse)
async def me(
    user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    """Return the current authenticated user."""
    return _user_response(user)
