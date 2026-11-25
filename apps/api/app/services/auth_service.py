"""Business logic for email/password registration and login."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import APIError, AuthenticationError, PermissionDeniedError
from app.core.logging import get_logger
from app.core.passwords import (
    hash_password,
    hash_password_async,
    password_needs_rehash,
    verify_password_async,
)
from app.core.security import create_access_token
from app.db.models import User

logger = get_logger(__name__)


class EmailExistsError(APIError):
    """The email is already registered."""

    status_code = 409
    error_type = "invalid_request_error"
    code = "email_exists"


class InvalidCredentialsError(AuthenticationError):
    """Unknown email, wrong password, or a non-local account.

    The message is intentionally generic so it never reveals whether the
    email exists.
    """

    code = "invalid_credentials"


class UserSuspendedError(PermissionDeniedError):
    """A valid password was supplied, but the account is suspended."""

    code = "user_suspended"


def _normalise_email(email: str) -> str:
    return email.strip().lower()


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """Cached throwaway hash so unknown emails cost the same as wrong passwords.

    Computed once per process; never stored, returned, or logged.
    """
    return hash_password(uuid.uuid4().hex)


class AuthRepository(Protocol):
    """Persistence boundary for dashboard users (overridable in tests)."""

    async def get_by_email(self, email: str) -> User | None: ...

    async def get_by_id(self, user_id: uuid.UUID) -> User | None: ...

    async def create(self, user: User) -> None: ...

    async def flush(self) -> None: ...

    async def refresh(self, user: User) -> None: ...

    async def rollback(self) -> None: ...


class SQLAlchemyAuthRepository:
    """Auth repository backed by an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def create(self, user: User) -> None:
        self._session.add(user)

    async def flush(self) -> None:
        await self._session.flush()

    async def refresh(self, user: User) -> None:
        await self._session.refresh(user)

    async def rollback(self) -> None:
        await self._session.rollback()


class AuthService:
    """Email/password registration and login."""

    def __init__(
        self,
        repository: AuthRepository,
        settings: Settings,
        dummy_hash: str | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._dummy_hash = dummy_hash

    async def register(self, email: str, password: str, display_name: str | None) -> User:
        """Create a local account inside the request's single transaction."""
        email = _normalise_email(email)
        existing = await self._repository.get_by_email(email)
        if existing is not None:
            raise EmailExistsError("Email already registered")
        password_hash = await hash_password_async(password)
        now = datetime.now(UTC)
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=display_name.strip() if display_name else None,
            password_hash=password_hash,
            role="user",
            status="active",
            spend_cap_monthly_cents=0,
            created_at=now,
            updated_at=now,
        )
        await self._repository.create(user)
        try:
            await self._repository.flush()
        except IntegrityError:
            # Concurrent registration won the unique constraint: clean 409.
            await self._repository.rollback()
            raise EmailExistsError("Email already registered") from None
        await self._repository.refresh(user)
        logger.info("auth_registered", user_id=str(user.id))
        return user

    async def _dummy_verify(self, password: str) -> None:
        """Equalise work on unknown emails without revealing account existence."""
        await verify_password_async(password, self._dummy_hash or dummy_password_hash())

    async def login(self, email: str, password: str) -> User:
        email = _normalise_email(email)
        user = await self._repository.get_by_email(email)
        if user is None or not user.password_hash:
            await self._dummy_verify(password)
            raise InvalidCredentialsError("Invalid email or password")
        valid = await verify_password_async(password, user.password_hash)
        if not valid:
            raise InvalidCredentialsError("Invalid email or password")
        if user.status == "suspended":
            raise UserSuspendedError("Account is suspended")
        if user.status != "active":
            raise InvalidCredentialsError("Invalid email or password")
        if password_needs_rehash(user.password_hash):
            user.password_hash = await hash_password_async(password)
        user.last_login_at = datetime.now(UTC)
        await self._repository.flush()
        logger.info("auth_login_succeeded", user_id=str(user.id))
        return user

    def issue_token(self, user: User) -> tuple[str, int]:
        """Return (token, seconds-to-expiry) for a freshly authenticated user."""
        minutes = self._settings.jwt_access_token_expire_minutes
        token = create_access_token(
            str(user.id),
            self._settings.jwt_secret,
            self._settings.jwt_issuer,
            self._settings.jwt_audience,
            minutes,
        )
        return token, minutes * 60

    async def resolve_user(self, user_id: uuid.UUID) -> User | None:
        return await self._repository.get_by_id(user_id)


__all__ = [
    "AuthRepository",
    "AuthService",
    "EmailExistsError",
    "InvalidCredentialsError",
    "SQLAlchemyAuthRepository",
    "UserSuspendedError",
    "dummy_password_hash",
]
