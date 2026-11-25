"""In-memory test doubles; no external services are contacted."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.db.models import APIKey, AuditLog, User


class FakeAuthRepository:
    """Simple async user store with optional unique-race simulation."""

    def __init__(self, users: dict[uuid.UUID, User] | None = None) -> None:
        self.users = users if users is not None else {}
        self.rollbacks = 0
        self.fail_next_flush = False

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self.users.values() if u.email == email.lower()), None)

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    async def create(self, user: User) -> None:
        self.users[user.id] = user

    async def flush(self) -> None:
        if self.fail_next_flush:
            self.fail_next_flush = False
            raise IntegrityError("INSERT", {"email": "x"}, Exception("unique violation"))

    async def refresh(self, user: User) -> None:
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeAPIKeyRepository:
    """In-memory API-key and audit-log store."""

    def __init__(self) -> None:
        self.keys: dict[uuid.UUID, APIKey] = {}
        self.users: dict[uuid.UUID, User] = {}
        self.audits: list[AuditLog] = []
        self.get_by_hash_calls = 0
        self.flushes = 0

    def register_user(self, user: User) -> None:
        self.users[user.id] = user

    async def get_by_hash(self, key_hash: str) -> tuple[APIKey, User] | None:
        self.get_by_hash_calls += 1
        for key in self.keys.values():
            if key.key_hash == key_hash:
                return key, self.users[key.user_id]
        return None

    async def get_by_id(self, key_id: uuid.UUID) -> APIKey | None:
        return self.keys.get(key_id)

    async def list_for_user(self, user_id: uuid.UUID) -> list[APIKey]:
        return [key for key in self.keys.values() if key.user_id == user_id]

    async def create(self, key: APIKey) -> None:
        self.keys[key.id] = key

    async def create_audit(self, log: AuditLog) -> None:
        self.audits.append(log)

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, key: APIKey) -> None:
        return None


class FakeSession:
    """Minimal async session used only for ``session.get(User, id)``."""

    def __init__(self, users: dict[uuid.UUID, User] | None = None) -> None:
        self.users = users if users is not None else {}

    async def get(self, model: Any, key: Any) -> Any:
        if model is User:
            return self.users.get(key)
        return None


class FakeRedis:
    """In-memory Redis stand-in with cache-hit counters and failure modes."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.gets = 0
        self.sets = 0
        self.deletes: list[str] = []
        self.fail = fail

    async def get(self, key: str) -> bytes | None:
        self.gets += 1
        if self.fail:
            raise RuntimeError("redis unavailable")
        return self.store.get(key)

    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> None:
        self.sets += 1
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.store[key] = value.encode() if isinstance(value, str) else value
        if ex is not None:
            self.ttls[key] = ex

    async def delete(self, key: str) -> None:
        self.deletes.append(key)
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.store.pop(key, None)
        self.ttls.pop(key, None)


def make_user(
    *,
    email: str = "user@example.com",
    password_hash: str | None = None,
    role: str = "user",
    status: str = "active",
    user_id: uuid.UUID | None = None,
) -> User:
    now = datetime.now(UTC)
    return User(
        id=user_id or uuid.uuid4(),
        email=email,
        display_name="Test User",
        password_hash=password_hash,
        role=role,
        status=status,
        spend_cap_monthly_cents=0,
        created_at=now,
        updated_at=now,
    )
