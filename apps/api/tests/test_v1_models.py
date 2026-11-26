"""Tests for GET /v1/models (allowlist, OpenAI shape, scope enforcement)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import get_api_key_principal, get_api_key_repository
from app.db.models import ModelPricing
from app.services.api_key_service import APIKeyPrincipal
from app.services.model_pricing import ModelPricingRepository, get_model_pricing_repository
from tests.fakes import FakeAPIKeyRepository


def _row(model_id: str, *, enabled: bool = True, provider: str = "litellm") -> ModelPricing:
    now = datetime.now(UTC)
    return ModelPricing(
        id=uuid.uuid4(),
        model_id=model_id,
        input_price_per_1k=0,
        output_price_per_1k=0,
        enabled=enabled,
        provider=provider,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )


class _Repo:
    def __init__(self, rows: list[ModelPricing]) -> None:
        self._rows = rows

    async def list_enabled(self) -> list[ModelPricing]:
        return [row for row in self._rows if row.enabled]

    async def get_enabled(self, model_id: str) -> ModelPricing | None:
        return next((row for row in self._rows if row.model_id == model_id and row.enabled), None)


def _principal(*scopes: str) -> APIKeyPrincipal:
    return APIKeyPrincipal(
        user_id=str(uuid.uuid4()),
        api_key_id=str(uuid.uuid4()),
        scopes=scopes,
    )


def _client(
    app: FastAPI, *, principal: APIKeyPrincipal | None, rows: list[ModelPricing]
) -> TestClient:
    repository: ModelPricingRepository = _Repo(rows)
    app.dependency_overrides[get_model_pricing_repository] = lambda: repository
    if principal is not None:
        app.dependency_overrides[get_api_key_principal] = lambda: principal
    else:
        app.dependency_overrides[get_api_key_repository] = lambda: FakeAPIKeyRepository()
    return TestClient(app, raise_server_exceptions=False)


def test_models_requires_api_key(app: FastAPI) -> None:
    client = _client(app, principal=None, rows=[_row("gateway-default")])
    response = client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_models_requires_models_read_scope(app: FastAPI) -> None:
    client = _client(app, principal=_principal("chat:completions"), rows=[_row("gateway-default")])
    response = client.get("/v1/models")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_models_lists_only_enabled_rows_in_openai_shape(app: FastAPI) -> None:
    client = _client(
        app,
        principal=_principal("models:read"),
        rows=[
            _row("gateway-default", provider="litellm"),
            _row("disabled-model", enabled=False),
        ],
    )
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [item["id"] for item in body["data"]] == ["gateway-default"]
    item = body["data"][0]
    assert item["object"] == "model"
    assert item["owned_by"] == "litellm"
    assert isinstance(item["created"], int)
    assert "disabled-model" not in response.text


def test_models_empty_allowlist(app: FastAPI) -> None:
    client = _client(app, principal=_principal("models:read"), rows=[])
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}
