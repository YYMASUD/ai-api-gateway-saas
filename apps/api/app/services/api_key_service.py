"""API-key creation, listing, revocation, and authentication caching.

Keys are ``sk_live_`` plus 43 base62 characters encoding 32 random bytes.
Only the peppered SHA-256 hash and an 8-character random-part prefix are
persisted; the full key is returned exactly once at creation and is never
stored, logged, or returned again.
"""

from __future__ import annotations

import json
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import AuthenticationError, NotFoundError
from app.core.logging import get_logger
from app.core.security import hash_api_key
from app.db.models import APIKey, AuditLog, User

logger = get_logger(__name__)

API_KEY_CACHE_TTL_SECONDS = 60
_KEY_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
_KEY_FORMAT_PREFIX = "sk_live_"
_KEY_RANDOM_LENGTH = 43
_KEY_PREFIX_LENGTH = 8


@dataclass(frozen=True)
class GeneratedKey:
    """One-time generation result; only hash and prefix belong in the DB."""

    raw_key: str
    prefix: str
    key_hash: str


def _base62(raw: bytes, length: int = _KEY_RANDOM_LENGTH) -> str:
    """Encode random bytes as fixed-length, zero-padded base62."""
    value = int.from_bytes(raw, "big")
    chars: list[str] = []
    while value:
        value, index = divmod(value, len(_KEY_ALPHABET))
        chars.append(_KEY_ALPHABET[index])
    encoded = "".join(reversed(chars)) or "0"
    return encoded.rjust(length, "0")


def generate_api_key(pepper: str) -> GeneratedKey:
    """Generate ``sk_live_<43 base62 chars>`` and its peppered SHA-256 hash."""
    random_part = _base62(secrets.token_bytes(32))
    raw_key = f"{_KEY_FORMAT_PREFIX}{random_part}"
    return GeneratedKey(
        raw_key=raw_key,
        prefix=random_part[:_KEY_PREFIX_LENGTH],
        key_hash=hash_api_key(raw_key, pepper),
    )


def display_prefix(stored_prefix: str) -> str:
    """Render the stored random-part prefix as ``sk_live_XXXXXXXX``."""
    return f"{_KEY_FORMAT_PREFIX}{stored_prefix}"


def cache_key_for_hash(key_hash: str) -> str:
    """Return the Redis key for a key's SHA-256 hex digest."""
    return f"apikey:{key_hash}"


def client_ip(request: Request) -> str | None:
    """Return the immediate client IP for audit logging, never a token."""
    return request.client.host if request.client else None


@dataclass(frozen=True)
class APIKeyPrincipal:
    """Authenticated API-key identity, safe to cache as JSON."""

    user_id: str
    api_key_id: str
    scopes: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "user_id": self.user_id,
                "api_key_id": self.api_key_id,
                "scopes": list(self.scopes),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> APIKeyPrincipal | None:
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict):
                return None
            user_id = payload.get("user_id")
            api_key_id = payload.get("api_key_id")
            scopes = payload.get("scopes")
            if (
                not isinstance(user_id, str)
                or not isinstance(api_key_id, str)
                or not isinstance(scopes, list)
                or not all(isinstance(scope, str) for scope in scopes)
            ):
                return None
            uuid.UUID(user_id)
            uuid.UUID(api_key_id)
            return cls(user_id=user_id, api_key_id=api_key_id, scopes=tuple(scopes))
        except (ValueError, TypeError):
            return None


class APIKeyRepository(Protocol):
    """Persistence boundary for API keys and audit logs."""

    async def get_by_hash(self, key_hash: str) -> tuple[APIKey, User] | None: ...

    async def get_by_id(self, key_id: uuid.UUID) -> APIKey | None: ...

    async def list_for_user(self, user_id: uuid.UUID) -> list[APIKey]: ...

    async def create(self, key: APIKey) -> None: ...

    async def create_audit(self, log: AuditLog) -> None: ...

    async def flush(self) -> None: ...

    async def refresh(self, key: APIKey) -> None: ...


class SQLAlchemyAPIKeyRepository:
    """API-key repository backed by an async SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, key_hash: str) -> tuple[APIKey, User] | None:
        result = await self._session.execute(
            select(APIKey, User).where(
                APIKey.key_hash == key_hash,
                APIKey.user_id == User.id,
            )
        )
        row = result.first()
        if row is None:
            return None
        key, user = row
        return key, user

    async def get_by_id(self, key_id: uuid.UUID) -> APIKey | None:
        return await self._session.get(APIKey, key_id)

    async def list_for_user(self, user_id: uuid.UUID) -> list[APIKey]:
        result = await self._session.execute(
            select(APIKey)
            .where(APIKey.user_id == user_id)
            .order_by(APIKey.created_at.desc(), APIKey.id.desc())
        )
        return list(result.scalars().all())

    async def create(self, key: APIKey) -> None:
        self._session.add(key)

    async def create_audit(self, log: AuditLog) -> None:
        self._session.add(log)

    async def flush(self) -> None:
        await self._session.flush()

    async def refresh(self, key: APIKey) -> None:
        await self._session.refresh(key)


class APIKeyService:
    """Create, list, revoke, and authenticate API keys."""

    def __init__(
        self,
        repository: APIKeyRepository,
        settings: Settings,
        redis: Redis | None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._redis = redis

    async def create_key(
        self,
        user_id: uuid.UUID,
        name: str,
        scopes: list[str],
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[APIKey, str]:
        """Persist hash+prefix and audit the creation; return the key once."""
        generated = generate_api_key(self._settings.api_key_pepper)
        now = datetime.now(UTC)
        key = APIKey(
            id=uuid.uuid4(),
            user_id=user_id,
            name=name,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=list(scopes),
            last_used_at=None,
            revoked_at=None,
            created_at=now,
            updated_at=now,
        )
        await self._repository.create(key)
        await self._repository.create_audit(
            AuditLog(
                id=uuid.uuid4(),
                actor_id=user_id,
                actor_type="user",
                action="api_key.create",
                target_type="api_key",
                target_id=str(key.id),
                ip=ip,
                user_agent=user_agent,
                metadata_json={"name": name, "scopes": list(scopes)},
                created_at=now,
                updated_at=now,
            )
        )
        await self._repository.flush()
        await self._repository.refresh(key)
        logger.info("api_key_created", key_id=str(key.id), user_id=str(user_id))
        return key, generated.raw_key

    async def list_keys(self, user_id: uuid.UUID) -> list[APIKey]:
        return await self._repository.list_for_user(user_id)

    async def purge_cache(self, key_hash: str) -> None:
        """Delete a cached principal; failures must not block revocation."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(cache_key_for_hash(key_hash))
        except Exception:
            logger.warning("api_key_cache_delete_failed")

    async def revoke_key(
        self,
        user_id: uuid.UUID,
        key_id: uuid.UUID,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Owner-only, idempotent revocation with audit and cache purge."""
        key = await self._repository.get_by_id(key_id)
        if key is None:
            raise NotFoundError("API key not found")
        if key.user_id != user_id:
            # Non-owners must see the same response as callers probing unknown
            # keys, otherwise revocation can be used to enumerate key IDs.
            raise NotFoundError("API key not found")
        if key.revoked_at is None:
            now = datetime.now(UTC)
            key.revoked_at = now
            key.updated_at = now
            await self._repository.create_audit(
                AuditLog(
                    id=uuid.uuid4(),
                    actor_id=user_id,
                    actor_type="user",
                    action="api_key.revoke",
                    target_type="api_key",
                    target_id=str(key.id),
                    ip=ip,
                    user_agent=user_agent,
                    metadata_json={"name": key.name, "scopes": list(key.scopes)},
                    created_at=now,
                    updated_at=now,
                )
            )
            await self._repository.flush()
            logger.info("api_key_revoked", key_id=str(key.id), user_id=str(user_id))
        # Purge even if already revoked: an earlier delete may have failed.
        await self.purge_cache(key.key_hash)

    def _invalid_key(self) -> AuthenticationError:
        return AuthenticationError("Invalid API key", code="invalid_api_key")

    async def authenticate(self, raw_key: str) -> APIKeyPrincipal:
        """Verify an API key, using a positive 60s Redis cache when available.

        ``last_used_at`` is written only on cache misses, so its granularity
        is approximately the 60s cache TTL. Redis failures fail open to the
        database; cache problems never break authentication and never log
        secrets.
        """
        if not isinstance(raw_key, str) or not raw_key.startswith(_KEY_FORMAT_PREFIX):
            raise self._invalid_key()
        random_part = raw_key[len(_KEY_FORMAT_PREFIX) :]
        if len(random_part) != _KEY_RANDOM_LENGTH or any(
            char not in _KEY_ALPHABET for char in random_part
        ):
            raise self._invalid_key()
        key_hash = hash_api_key(raw_key, self._settings.api_key_pepper)
        cache_key = cache_key_for_hash(key_hash)

        if self._redis is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached is not None:
                    principal = APIKeyPrincipal.from_json(cached)
                    if principal is not None:
                        return principal
            except Exception:
                # Fail open: a Redis outage must not break API-key auth.
                logger.warning("api_key_cache_read_failed")

        pair = await self._repository.get_by_hash(key_hash)
        if pair is None:
            raise self._invalid_key()
        key, user = pair
        if key.revoked_at is not None:
            raise self._invalid_key()
        if user.status == "suspended":
            # Suspended users must be indistinguishable from invalid/revoked keys
            # to avoid leaking account existence and suspension state.
            raise self._invalid_key()
        if user.status != "active":
            raise self._invalid_key()
        # Record activity only on DB misses; cached requests deliberately skip
        # both this write and the repository lookup. Granularity is therefore
        # approximately the positive principal cache TTL.
        key.last_used_at = datetime.now(UTC)
        await self._repository.flush()
        principal = APIKeyPrincipal(
            user_id=str(user.id),
            api_key_id=str(key.id),
            scopes=tuple(key.scopes),
        )
        if self._redis is not None:
            try:
                await self._redis.set(cache_key, principal.to_json(), ex=API_KEY_CACHE_TTL_SECONDS)
            except Exception:
                # Cache write failure is non-fatal; the DB lookup succeeded.
                logger.warning("api_key_cache_write_failed")
        return principal


__all__ = [
    "API_KEY_CACHE_TTL_SECONDS",
    "APIKeyPrincipal",
    "APIKeyRepository",
    "APIKeyService",
    "GeneratedKey",
    "SQLAlchemyAPIKeyRepository",
    "cache_key_for_hash",
    "client_ip",
    "display_prefix",
    "generate_api_key",
]
