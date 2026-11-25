"""Auth service unit tests with in-memory repositories (no database)."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from app.config import Settings
from app.core.passwords import hash_password, password_needs_rehash, verify_password
from app.core.security import verify_jwt
from app.services.auth_service import (
    AuthService,
    EmailExistsError,
    InvalidCredentialsError,
    UserSuspendedError,
)
from tests.fakes import FakeAuthRepository, make_user

PASSWORD = "correct horse battery staple"


async def test_register_normalises_email_and_hashes_password(settings: Settings) -> None:
    repo = FakeAuthRepository()
    service = AuthService(repo, settings)
    user = await service.register("  USER@Example.COM ", PASSWORD, "  Display  ")
    assert user.email == "user@example.com"
    assert user.display_name == "Display"
    assert user.password_hash is not None
    assert user.password_hash.startswith("$argon2id$")
    assert user.password_hash != PASSWORD
    assert verify_password(PASSWORD, user.password_hash)
    assert user.role == "user"
    assert user.status == "active"


async def test_register_duplicate_email_conflict(settings: Settings) -> None:
    repo = FakeAuthRepository()
    existing = make_user(email="user@example.com")
    repo.users[existing.id] = existing
    service = AuthService(repo, settings)
    with pytest.raises(EmailExistsError) as exc:
        await service.register("USER@example.com", PASSWORD, None)
    assert exc.value.status_code == 409
    assert exc.value.code == "email_exists"


async def test_register_unique_race_returns_conflict_and_rolls_back(settings: Settings) -> None:
    repo = FakeAuthRepository()
    repo.fail_next_flush = True
    service = AuthService(repo, settings)
    with pytest.raises(EmailExistsError) as exc:
        await service.register("user@example.com", PASSWORD, None)
    assert exc.value.code == "email_exists"
    assert repo.rollbacks == 1


async def test_login_success_sets_last_login(settings: Settings) -> None:
    repo = FakeAuthRepository()
    user = make_user(password_hash=hash_password(PASSWORD))
    repo.users[user.id] = user
    service = AuthService(repo, settings)
    resolved = await service.login(" USER@example.com ", PASSWORD)
    assert resolved is user
    assert user.last_login_at is not None


async def test_login_invalid_credentials_is_generic(settings: Settings) -> None:
    repo = FakeAuthRepository()
    user = make_user(password_hash=hash_password(PASSWORD))
    repo.users[user.id] = user
    service = AuthService(repo, settings)
    with pytest.raises(InvalidCredentialsError) as wrong:
        await service.login(user.email, "wrong password 123")
    with pytest.raises(InvalidCredentialsError) as unknown:
        await service.login("nobody@example.com", PASSWORD)
    assert wrong.value.status_code == unknown.value.status_code == 401
    assert wrong.value.code == unknown.value.code == "invalid_credentials"
    assert wrong.value.message == unknown.value.message


async def test_login_suspended_user_forbidden(settings: Settings) -> None:
    repo = FakeAuthRepository()
    user = make_user(password_hash=hash_password(PASSWORD), status="suspended")
    repo.users[user.id] = user
    service = AuthService(repo, settings)
    with pytest.raises(UserSuspendedError) as exc:
        await service.login(user.email, PASSWORD)
    assert exc.value.status_code == 403
    assert exc.value.code == "user_suspended"


async def test_login_without_local_password_rejected(settings: Settings) -> None:
    repo = FakeAuthRepository()
    user = make_user(password_hash=None)
    repo.users[user.id] = user
    service = AuthService(repo, settings)
    with pytest.raises(InvalidCredentialsError):
        await service.login(user.email, PASSWORD)


async def test_login_rehashes_outdated_parameters(settings: Settings) -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    repo = FakeAuthRepository()
    user = make_user(password_hash=weak)
    repo.users[user.id] = user
    service = AuthService(repo, settings)
    await service.login(user.email, PASSWORD)
    assert user.password_hash != weak
    assert user.password_hash is not None
    assert password_needs_rehash(user.password_hash) is False
    assert verify_password(PASSWORD, user.password_hash)


async def test_issue_token_claims_and_expiry(settings: Settings) -> None:
    user = make_user()
    service = AuthService(FakeAuthRepository(), settings)
    token, expires_in = service.issue_token(user)
    assert expires_in == settings.jwt_access_token_expire_minutes * 60
    claims = verify_jwt(token, settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience)
    assert claims["sub"] == str(user.id)
    assert claims["type"] == "access"
