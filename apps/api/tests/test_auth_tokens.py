"""Access-token creation/verification and auth-dependency tests."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any

import jwt
import pytest

from app.config import Settings
from app.core.auth import (
    get_api_key_principal,
    get_current_user,
    require_admin,
    verify_access_token_claims,
)
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import (
    ACCESS_TOKEN_TYPE,
    JWT_ALGORITHM,
    TokenVerificationError,
    create_access_token,
    verify_jwt,
)
from app.services.api_key_service import APIKeyPrincipal, APIKeyService
from tests.fakes import FakeAPIKeyRepository, FakeRedis, FakeSession, make_user

SECRET = "test-jwt-secret-value-with-enough-entropy"
ISSUER = "ai-api-gateway"
AUDIENCE = "ai-api-gateway-dashboard"


def test_create_access_token_has_all_required_claims() -> None:
    subject = str(uuid.uuid4())
    token = create_access_token(subject, SECRET, ISSUER, AUDIENCE, 30)
    claims = verify_jwt(token, SECRET, ISSUER, AUDIENCE)
    assert claims["sub"] == subject
    assert claims["iss"] == ISSUER
    assert claims["aud"] == AUDIENCE
    assert claims["type"] == ACCESS_TOKEN_TYPE
    assert claims["jti"]
    assert uuid.UUID(claims["jti"])
    assert claims["exp"] - claims["iat"] == 30 * 60


def test_create_access_token_is_hs256_signed() -> None:
    token = create_access_token(str(uuid.uuid4()), SECRET, ISSUER, AUDIENCE, 5)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == JWT_ALGORITHM


def test_create_access_token_rejects_missing_parameters() -> None:
    with pytest.raises(ValueError):
        create_access_token("", SECRET, ISSUER, AUDIENCE, 5)
    with pytest.raises(ValueError):
        create_access_token(str(uuid.uuid4()), "", ISSUER, AUDIENCE, 5)
    with pytest.raises(ValueError):
        create_access_token(str(uuid.uuid4()), SECRET, ISSUER, AUDIENCE, 0)


def test_verify_access_token_claims_requires_access_type() -> None:
    user_id = uuid.uuid4()
    assert verify_access_token_claims({"sub": str(user_id), "type": "access"}) == user_id
    with pytest.raises(TokenVerificationError):
        verify_access_token_claims({"sub": str(user_id), "type": "refresh"})
    with pytest.raises(TokenVerificationError):
        verify_access_token_claims({"type": "access"})
    with pytest.raises(TokenVerificationError):
        verify_access_token_claims({"sub": "not-a-uuid", "type": "access"})


def _bearer(token: str) -> SimpleNamespace:
    return SimpleNamespace(credentials=token, scheme="Bearer")


def _request(settings: Settings, redis: FakeRedis | None = None) -> Any:
    state = SimpleNamespace(settings=settings, redis=redis)
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _current_user(settings: Settings, session: FakeSession, token: str | None) -> Any:
    credentials = _bearer(token) if token is not None else None
    return await get_current_user(_request(settings), credentials, session)  # type: ignore[arg-type]


async def test_get_current_user_returns_active_user(settings: Settings) -> None:
    user = make_user()
    token = create_access_token(
        str(user.id), settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience, 5
    )
    resolved = await _current_user(settings, FakeSession({user.id: user}), token)
    assert resolved is user


async def test_get_current_user_rejects_missing_or_invalid_tokens(settings: Settings) -> None:
    session = FakeSession()
    with pytest.raises(AuthenticationError) as missing:
        await _current_user(settings, session, None)
    assert missing.value.status_code == 401
    assert missing.value.code == "invalid_token"

    with pytest.raises(AuthenticationError):
        await _current_user(settings, session, "not-a-jwt")

    wrong_secret = create_access_token(str(uuid.uuid4()), "another-secret", ISSUER, AUDIENCE, 5)
    with pytest.raises(AuthenticationError):
        await _current_user(settings, session, wrong_secret)


async def test_get_current_user_rejects_expired_token(settings: Settings) -> None:
    now = int(time.time())
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now - 3600,
            "exp": now - 60,
            "jti": str(uuid.uuid4()),
            "type": "access",
        },
        settings.jwt_secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        await _current_user(settings, FakeSession(), expired)


async def test_get_current_user_rejects_non_access_token_type(settings: Settings) -> None:
    now = int(time.time())
    refresh_like = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now,
            "exp": now + 60,
            "type": "refresh",
        },
        settings.jwt_secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        await _current_user(settings, FakeSession(), refresh_like)


async def test_get_current_user_rejects_unknown_and_suspended_users(settings: Settings) -> None:
    unknown = create_access_token(
        str(uuid.uuid4()), settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience, 5
    )
    with pytest.raises(AuthenticationError):
        await _current_user(settings, FakeSession(), unknown)

    suspended = make_user(status="suspended")
    suspended_token = create_access_token(
        str(suspended.id), settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience, 5
    )
    with pytest.raises(PermissionDeniedError) as exc:
        await _current_user(settings, FakeSession({suspended.id: suspended}), suspended_token)
    assert exc.value.status_code == 403
    assert exc.value.code == "user_suspended"


async def test_require_admin_enforces_role() -> None:
    admin = make_user(role="admin")
    assert await require_admin(admin) is admin
    with pytest.raises(PermissionDeniedError) as exc:
        await require_admin(make_user(role="user"))
    assert exc.value.status_code == 403


async def test_api_key_principal_dependency_uses_repository(settings: Settings) -> None:
    user = make_user()
    repository = FakeAPIKeyRepository()
    repository.register_user(user)
    service = APIKeyService(repository, settings, FakeRedis())
    key, raw_key = await service.create_key(user.id, "CI", ["models:read"])

    principal = await get_api_key_principal(
        _request(settings),
        _bearer(raw_key),
        repository,  # type: ignore[arg-type]
    )
    assert isinstance(principal, APIKeyPrincipal)
    assert principal.user_id == str(user.id)
    assert principal.api_key_id == str(key.id)
    assert principal.scopes == ("models:read",)


async def test_api_key_principal_dependency_rejects_missing_credentials(
    settings: Settings,
) -> None:
    repository = FakeAPIKeyRepository()
    with pytest.raises(AuthenticationError):
        await get_api_key_principal(
            _request(settings),
            None,
            repository,  # type: ignore[arg-type]
        )
