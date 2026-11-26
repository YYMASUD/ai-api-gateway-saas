"""Dashboard credit endpoint tests with dependency overrides (no DB)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.auth import get_current_user
from app.core.security import create_access_token
from app.db.models import CreditBalance, CreditLedger, User
from app.db.session import get_db
from app.routers.credits import get_credit_repository
from app.services.credit_service import CreditRepository
from tests.fakes import FakeSession, make_user
from tests.test_credit_service import FakeCreditRepository


def _client(app: FastAPI, repo: CreditRepository, user: User) -> TestClient:
    app.dependency_overrides[get_credit_repository] = lambda: repo
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _token(settings: Settings, user: User) -> str:
    token, _ = _issued_token(settings, user)
    return token


def _issued_token(settings: Settings, user: User) -> tuple[str, int]:
    return create_access_token(
        str(user.id),
        settings.jwt_secret,
        settings.jwt_issuer,
        settings.jwt_audience,
        settings.jwt_access_token_expire_minutes,
    ), settings.jwt_access_token_expire_minutes * 60


def _db_override(app: FastAPI, users: list[User] | None = None) -> None:
    store = {user.id: user for user in users or []}

    async def _fake_db() -> AsyncIterator[FakeSession]:
        yield FakeSession(store)

    app.dependency_overrides[get_db] = _fake_db


def _ledger(
    user_id: uuid.UUID,
    *,
    amount: int,
    created_at: datetime,
    kind: str = "purchase",
    reference_id: str = "ref",
    metadata: dict | None = None,
) -> CreditLedger:
    return CreditLedger(
        id=uuid.uuid4(),
        user_id=user_id,
        type=kind,
        amount_microcredits=amount,
        reference_id=reference_id,
        idempotency_key=f"key-{uuid.uuid4()}",
        metadata_json=metadata or {},
        created_at=created_at,
        updated_at=created_at,
    )


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------


def test_get_balance_returns_documented_envelope(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    row = repo.seed_balance(user.id, 1000, reserved=300)
    row.lifetime_purchased_microcredits = 5000
    row.lifetime_spent_microcredits = 2000
    client = _client(app, repo, user)

    response = client.get("/dashboard/credits")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "balance_microcredits": 1000,
        "reserved_microcredits": 300,
        "available_microcredits": 700,
        "lifetime_purchased_microcredits": 5000,
        "lifetime_spent_microcredits": 2000,
    }


def test_get_balance_new_user_is_all_zeros(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    client = _client(app, repo, user)

    response = client.get("/dashboard/credits")

    assert response.status_code == 200
    assert response.json() == {
        "balance_microcredits": 0,
        "reserved_microcredits": 0,
        "available_microcredits": 0,
        "lifetime_purchased_microcredits": 0,
        "lifetime_spent_microcredits": 0,
    }
    assert user.id not in repo.balances


def test_credits_require_authentication(app: FastAPI) -> None:
    _db_override(app)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/dashboard/credits")

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["code"] == "invalid_token"


def test_invalid_bearer_token_is_401_envelope(app: FastAPI) -> None:
    _db_override(app)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(
        "/dashboard/credits", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


def test_suspended_user_is_403_envelope(app: FastAPI, settings: Settings) -> None:
    user = make_user(status="suspended")
    _db_override(app, [user])
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(
        "/dashboard/credits", headers={"Authorization": f"Bearer {_token(settings, user)}"}
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["type"] == "permission_error"
    assert body["error"]["code"] == "user_suspended"


def test_valid_jwt_resolves_active_user(app: FastAPI, settings: Settings) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    repo.seed_balance(user.id, 42)
    _db_override(app, [user])
    app.dependency_overrides[get_credit_repository] = lambda: repo
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(
        "/dashboard/credits", headers={"Authorization": f"Bearer {_token(settings, user)}"}
    )

    assert response.status_code == 200
    assert response.json()["balance_microcredits"] == 42


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


def test_list_ledger_orders_newest_first_by_created_at(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    oldest = _ledger(user.id, amount=10, created_at=base, reference_id="oldest")
    middle = _ledger(
        user.id, amount=5, created_at=base + timedelta(minutes=1), reference_id="middle"
    )
    newest = _ledger(
        user.id, amount=7, created_at=base + timedelta(minutes=2), reference_id="newest"
    )
    repo.ledgers.extend([oldest, middle, newest])
    client = _client(app, repo, user)

    response = client.get("/dashboard/credits/ledger?limit=2")

    assert response.status_code == 200
    body = response.json()
    # Ordering is by created_at/id descending, independent of amount.
    assert [item["reference_id"] for item in body["items"]] == ["newest", "middle"]
    assert body["next_cursor"] == str(middle.id)


def test_list_ledger_returns_documented_entry_shape(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    entry = _ledger(
        user.id,
        amount=-25,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        kind="settlement",
        reference_id="req-1",
        metadata={"reservation_id": "abc"},
    )
    repo.ledgers.append(entry)
    client = _client(app, repo, user)

    response = client.get("/dashboard/credits/ledger")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert set(item) == {
        "id",
        "type",
        "amount_microcredits",
        "reference_id",
        "metadata",
        "created_at",
    }
    assert item["id"] == str(entry.id)
    assert item["type"] == "settlement"
    assert item["amount_microcredits"] == -25
    assert item["reference_id"] == "req-1"
    assert item["metadata"] == {"reservation_id": "abc"}


def test_list_ledger_paginates_with_cursor(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [
        _ledger(
            user.id,
            amount=1000 + index,
            created_at=base + timedelta(minutes=index),
            reference_id=f"ref-{index}",
        )
        for index in range(3)
    ]
    repo.ledgers.extend(entries)
    client = _client(app, repo, user)

    first = client.get("/dashboard/credits/ledger?limit=2").json()
    assert [item["reference_id"] for item in first["items"]] == ["ref-2", "ref-1"]
    assert first["next_cursor"] == str(entries[1].id)

    second = client.get(f"/dashboard/credits/ledger?limit=2&cursor={first['next_cursor']}").json()
    assert [item["reference_id"] for item in second["items"]] == ["ref-0"]
    assert second["next_cursor"] is None


def test_list_ledger_owner_only(app: FastAPI) -> None:
    owner = make_user()
    other = make_user(email="other@example.com")
    repo = FakeCreditRepository()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repo.ledgers.append(_ledger(owner.id, amount=1000, created_at=now, reference_id="owner-ref"))
    repo.ledgers.append(_ledger(other.id, amount=2000, created_at=now, reference_id="other-ref"))
    client = _client(app, repo, owner)

    response = client.get("/dashboard/credits/ledger")

    assert response.status_code == 200
    body = response.json()
    assert [item["reference_id"] for item in body["items"]] == ["owner-ref"]


def test_list_ledger_ignores_cursor_from_another_user(app: FastAPI) -> None:
    owner = make_user()
    other = make_user(email="other@example.com")
    repo = FakeCreditRepository()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    owner_entry = _ledger(owner.id, amount=10, created_at=base, reference_id="owner")
    other_entry = _ledger(
        other.id, amount=20, created_at=base + timedelta(minutes=1), reference_id="other"
    )
    repo.ledgers.extend([owner_entry, other_entry])
    client = _client(app, repo, owner)

    response = client.get(f"/dashboard/credits/ledger?cursor={other_entry.id}")

    assert response.status_code == 200
    body = response.json()
    assert [item["reference_id"] for item in body["items"]] == ["owner"]


def test_list_ledger_empty(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    client = _client(app, repo, user)

    response = client.get("/dashboard/credits/ledger")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_list_ledger_enforces_limit_bounds(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    client = _client(app, repo, user)

    for limit in (0, -1, 101):
        response = client.get(f"/dashboard/credits/ledger?limit={limit}")
        assert response.status_code == 422


def test_ledger_requires_authentication(app: FastAPI) -> None:
    _db_override(app)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/dashboard/credits/ledger")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_get_balance_omits_internal_balance_id(app: FastAPI) -> None:
    user = make_user()
    repo = FakeCreditRepository()
    row: CreditBalance = repo.seed_balance(user.id, 1)
    client = _client(app, repo, user)

    response = client.get("/dashboard/credits")

    assert str(row.id) not in response.text
    assert "user_id" not in response.text
