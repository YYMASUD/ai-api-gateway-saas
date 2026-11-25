"""/auth endpoint tests using dependency overrides (no DB or Redis)."""

from __future__ import annotations

import time
import uuid
from typing import Annotated

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.auth import get_current_user, require_admin
from app.core.passwords import hash_password
from app.core.security import JWT_ALGORITHM, create_access_token, verify_jwt
from app.db.models import User
from app.db.session import get_db
from app.routers.auth import get_auth_repository
from tests.fakes import FakeAuthRepository, FakeSession, make_user

PASSWORD = "correct horse battery staple"


def _client(
    app: FastAPI,
    repo: FakeAuthRepository,
    session: FakeSession | None = None,
) -> TestClient:
    app.dependency_overrides[get_auth_repository] = lambda: repo

    async def _fake_db():
        yield session if session is not None else FakeSession()

    app.dependency_overrides[get_db] = _fake_db
    return TestClient(app, raise_server_exceptions=False)


def test_register_returns_token_envelope(app: FastAPI, settings: Settings) -> None:
    client = _client(app, FakeAuthRepository())
    response = client.post(
        "/auth/register",
        json={"email": "  New@Example.COM ", "password": PASSWORD, "display_name": " New User "},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["display_name"] == "New User"
    assert body["user"]["role"] == "user"
    assert body["user"]["status"] == "active"
    assert body["user"]["created_at"]
    uuid.UUID(body["user"]["id"])
    claims = verify_jwt(
        body["access_token"], settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience
    )
    assert claims["sub"] == body["user"]["id"]
    assert claims["type"] == "access"
    assert "password_hash" not in response.text


@pytest.mark.parametrize("password", ["short", "x" * 129])
def test_register_password_policy(app: FastAPI, password: str) -> None:
    client = _client(app, FakeAuthRepository())
    response = client.post("/auth/register", json={"email": "a@example.com", "password": password})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_register_invalid_email(app: FastAPI) -> None:
    client = _client(app, FakeAuthRepository())
    response = client.post("/auth/register", json={"email": "not-an-email", "password": PASSWORD})
    assert response.status_code == 422


def test_register_duplicate_email_409(app: FastAPI) -> None:
    repo = FakeAuthRepository()
    existing = make_user(email="taken@example.com")
    repo.users[existing.id] = existing
    client = _client(app, repo)
    response = client.post(
        "/auth/register", json={"email": "TAKEN@example.com", "password": PASSWORD}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_exists"


def test_login_returns_token_envelope(app: FastAPI, settings: Settings) -> None:
    repo = FakeAuthRepository()
    user = make_user(password_hash=hash_password(PASSWORD))
    repo.users[user.id] = user
    client = _client(app, repo)
    response = client.post(
        "/auth/login", json={"email": " USER@example.com ", "password": PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(user.id)
    assert body["expires_in"] == 3600
    claims = verify_jwt(
        body["access_token"], settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience
    )
    assert claims["sub"] == str(user.id)


def test_login_invalid_credentials_generic(app: FastAPI) -> None:
    repo = FakeAuthRepository()
    user = make_user(password_hash=hash_password(PASSWORD))
    repo.users[user.id] = user
    client = _client(app, repo)
    wrong = client.post("/auth/login", json={"email": user.email, "password": "wrong password 123"})
    unknown = client.post("/auth/login", json={"email": "ghost@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"


def test_login_short_wrong_password_is_generic_401(app: FastAPI) -> None:
    # An out-of-policy password must not leak account state via a 422.
    repo = FakeAuthRepository()
    user = make_user(password_hash=hash_password(PASSWORD))
    repo.users[user.id] = user
    client = _client(app, repo)
    response = client.post("/auth/login", json={"email": user.email, "password": "short"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_suspended_is_generic_401(app: FastAPI) -> None:
    # A valid password for a suspended account must be indistinguishable
    # from a wrong password (no account-existence/suspension oracle).
    repo = FakeAuthRepository()
    user = make_user(password_hash=hash_password(PASSWORD), status="suspended")
    repo.users[user.id] = user
    client = _client(app, repo)
    suspended = client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    wrong = client.post("/auth/login", json={"email": user.email, "password": "wrong password 123"})
    assert suspended.status_code == wrong.status_code == 401
    assert suspended.json() == wrong.json()
    assert suspended.json()["error"]["code"] == "invalid_credentials"


def test_me_returns_user(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    client = _client(app, FakeAuthRepository(), FakeSession({user.id: user}))
    token = create_access_token(
        str(user.id), settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience, 5
    )
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
    assert "password_hash" not in response.text


def test_me_rejects_missing_token(app: FastAPI) -> None:
    client = _client(app, FakeAuthRepository(), FakeSession())
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_me_rejects_garbage_token(app: FastAPI) -> None:
    client = _client(app, FakeAuthRepository(), FakeSession())
    response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_me_rejects_expired_token(app: FastAPI, settings: Settings) -> None:
    now = int(time.time())
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now - 3600,
            "exp": now - 60,
            "type": "access",
        },
        settings.jwt_secret,
        algorithm=JWT_ALGORITHM,
    )
    client = _client(app, FakeAuthRepository(), FakeSession())
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_me_rejects_non_access_token(app: FastAPI, settings: Settings) -> None:
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
    client = _client(app, FakeAuthRepository(), FakeSession())
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {refresh_like}"})
    assert response.status_code == 401


def test_me_suspended_user_403(app: FastAPI, settings: Settings) -> None:
    user = make_user(status="suspended")
    client = _client(app, FakeAuthRepository(), FakeSession({user.id: user}))
    token = create_access_token(
        str(user.id), settings.jwt_secret, settings.jwt_issuer, settings.jwt_audience, 5
    )
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "user_suspended"


def test_require_admin_dependency(app: FastAPI) -> None:
    @app.get("/_test/admin-only")
    async def admin_only(user: Annotated[User, Depends(require_admin)]) -> dict:
        return {"role": user.role}

    app.dependency_overrides[get_current_user] = lambda: make_user(role="admin")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/_test/admin-only").status_code == 200

    app.dependency_overrides[get_current_user] = lambda: make_user(role="user")
    response = client.get("/_test/admin-only")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_register_and_login_do_not_log_secrets(
    app: FastAPI, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = FakeAuthRepository()
    client = _client(app, repo)
    reg = client.post("/auth/register", json={"email": "leak@example.com", "password": PASSWORD})
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    login = client.post("/auth/login", json={"email": "leak@example.com", "password": PASSWORD})
    assert login.status_code == 200
    captured = capsys.readouterr().out
    assert PASSWORD not in captured
    assert token not in captured
    assert settings.jwt_secret not in captured
    stored = next(iter(repo.users.values()))
    assert stored.password_hash is not None
    assert stored.password_hash not in captured
    assert PASSWORD not in reg.text
    assert PASSWORD not in login.text
