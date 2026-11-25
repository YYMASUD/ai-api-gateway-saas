"""Dashboard key endpoints with dependency overrides (no DB or Redis)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.auth import get_api_key_repository, get_current_user
from app.core.security import hash_api_key
from app.db.models import APIKey
from app.db.session import get_db
from app.services.api_key_service import cache_key_for_hash
from tests.fakes import FakeAPIKeyRepository, FakeRedis, FakeSession, make_user

KEY_PATTERN = re.compile(r"^sk_live_[0-9A-Za-z]{43}$")


def _client(
    app: FastAPI,
    repo: FakeAPIKeyRepository,
    user,
    redis: FakeRedis | None = None,
) -> TestClient:
    app.dependency_overrides[get_api_key_repository] = lambda: repo
    app.dependency_overrides[get_current_user] = lambda: user
    if redis is not None:
        app.state.redis = redis
    return TestClient(app, raise_server_exceptions=False)


def test_create_key_returns_full_key_once(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    repo = FakeAPIKeyRepository()
    repo.register_user(user)
    client = _client(app, repo, user)
    response = client.post("/dashboard/keys", json={"name": "  Production  "})
    assert response.status_code == 201
    body = response.json()
    assert KEY_PATTERN.fullmatch(body["key"])
    assert body["name"] == "Production"
    assert body["prefix"] == "sk_live_" + body["key"].removeprefix("sk_live_")[:8]
    assert body["scopes"] == ["chat:completions", "models:read"]
    assert body["revoked_at"] is None
    assert body["last_used_at"] is None
    stored = repo.keys[uuid.UUID(body["id"])]
    assert stored.key_hash == hash_api_key(body["key"], settings.api_key_pepper)
    assert stored.prefix == body["key"].removeprefix("sk_live_")[:8]
    assert repo.audits[0].action == "api_key.create"
    assert repo.audits[0].ip == "testclient"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "K", "scopes": ["admin:all"]},
        {"name": "   "},
        {"name": "x" * 65},
        {"name": "K", "scopes": []},
        {"name": ""},
    ],
)
def test_create_key_validation(app: FastAPI, payload: dict) -> None:
    user = make_user()
    repo = FakeAPIKeyRepository()
    repo.register_user(user)
    client = _client(app, repo, user)
    response = client.post("/dashboard/keys", json=payload)
    assert response.status_code == 422


def test_list_keys_never_leaks_secrets(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    repo = FakeAPIKeyRepository()
    repo.register_user(user)
    client = _client(app, repo, user)
    created = client.post("/dashboard/keys", json={"name": "K"}).json()
    response = client.get("/dashboard/keys")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert set(item) == {
        "id",
        "name",
        "prefix",
        "scopes",
        "last_used_at",
        "revoked_at",
        "created_at",
    }
    assert created["key"] not in response.text
    stored = repo.keys[uuid.UUID(item["id"])]
    assert stored.key_hash not in response.text
    assert item["prefix"] == created["prefix"]


def test_list_keys_scoped_to_owner(app: FastAPI) -> None:
    owner = make_user()
    other = make_user(email="other@example.com")
    repo = FakeAPIKeyRepository()
    repo.register_user(owner)
    repo.register_user(other)
    client = _client(app, repo, owner)
    created = client.post("/dashboard/keys", json={"name": "Mine"}).json()
    now = datetime.now(UTC)
    other_key = APIKey(
        id=uuid.uuid4(),
        user_id=other.id,
        name="Theirs",
        prefix="AAAAAAAA",
        key_hash="f" * 64,
        scopes=["models:read"],
        last_used_at=None,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    repo.keys[other_key.id] = other_key
    body = client.get("/dashboard/keys").json()
    assert [item["id"] for item in body] == [created["id"]]


def test_revoke_key_owner_only_idempotent_and_audited(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    repo = FakeAPIKeyRepository()
    repo.register_user(user)
    redis = FakeRedis()
    client = _client(app, repo, user, redis)
    created = client.post("/dashboard/keys", json={"name": "K"}).json()
    key_id = created["id"]
    stored = repo.keys[uuid.UUID(key_id)]
    redis.store[cache_key_for_hash(stored.key_hash)] = b"{}"
    response = client.delete(f"/dashboard/keys/{key_id}")
    assert response.status_code == 204
    assert response.content == b""
    assert stored.revoked_at is not None
    assert cache_key_for_hash(stored.key_hash) not in redis.store
    again = client.delete(f"/dashboard/keys/{key_id}")
    assert again.status_code == 204
    revoke_audits = [a for a in repo.audits if a.action == "api_key.revoke"]
    assert len(revoke_audits) == 1
    assert revoke_audits[0].actor_id == user.id
    assert revoke_audits[0].metadata_json == {
        "name": "K",
        "scopes": ["chat:completions", "models:read"],
    }


def test_revoke_other_users_key_not_found(app: FastAPI) -> None:
    # A non-owner must get the same 404 as an unknown key so revocation cannot
    # be used to enumerate other users' key IDs.
    owner = make_user()
    attacker = make_user(email="attacker@example.com")
    repo = FakeAPIKeyRepository()
    repo.register_user(owner)
    repo.register_user(attacker)
    client = _client(app, repo, owner)
    created = client.post("/dashboard/keys", json={"name": "K"}).json()
    app.dependency_overrides[get_current_user] = lambda: attacker
    response = client.delete(f"/dashboard/keys/{created['id']}")
    unknown = client.delete(f"/dashboard/keys/{uuid.uuid4()}")
    assert response.status_code == unknown.status_code == 404
    assert response.json() == unknown.json()
    assert response.json()["error"]["code"] == "not_found"
    assert repo.keys[uuid.UUID(created["id"])].revoked_at is None


def test_revoke_unknown_key_404(app: FastAPI) -> None:
    user = make_user()
    repo = FakeAPIKeyRepository()
    repo.register_user(user)
    client = _client(app, repo, user)
    response = client.delete(f"/dashboard/keys/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_revoke_invalid_uuid_422(app: FastAPI) -> None:
    user = make_user()
    repo = FakeAPIKeyRepository()
    repo.register_user(user)
    client = _client(app, repo, user)
    response = client.delete("/dashboard/keys/not-a-uuid")
    assert response.status_code == 422


def test_keys_require_authentication(app: FastAPI) -> None:
    async def _fake_db():
        yield FakeSession()

    app.dependency_overrides[get_db] = _fake_db
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/dashboard/keys").status_code == 401
    assert client.post("/dashboard/keys", json={"name": "K"}).status_code == 401


def test_create_key_not_logged(
    app: FastAPI, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    user = make_user()
    repo = FakeAPIKeyRepository()
    repo.register_user(user)
    client = _client(app, repo, user)
    created = client.post("/dashboard/keys", json={"name": "K"}).json()
    stored = repo.keys[uuid.UUID(created["id"])]
    captured = capsys.readouterr().out
    assert created["key"] not in captured
    assert stored.key_hash not in captured
