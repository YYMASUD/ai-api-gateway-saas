"""Request ID, CORS, and OpenAI-compatible error format tests."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import APIError, error_body, validation_error_handler


def test_request_id_header_is_generated_and_returned(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert uuid.UUID(response.headers["X-Request-ID"])


def test_request_id_header_is_echoed(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "req-123"})
    assert response.headers["X-Request-ID"] == "req-123"


def test_invalid_request_id_is_replaced(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "bad\nid"})
    assert response.headers["X-Request-ID"] != "bad\nid"
    assert uuid.UUID(response.headers["X-Request-ID"])


def test_dashboard_cors_allows_configured_origin(app: FastAPI, settings) -> None:
    with TestClient(app) as client:
        response = client.options(
            "/dashboard/session",
            headers={
                "Origin": settings.app_url,
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == settings.app_url
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_rejects_unconfigured_origin(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.options(
            "/dashboard/session",
            headers={
                "Origin": "https://attacker.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in response.headers


def test_public_v1_api_is_not_cors_enabled(app: FastAPI, settings) -> None:
    with TestClient(app) as client:
        response = client.options(
            "/v1/chat/completions",
            headers={
                "Origin": settings.app_url,
                "Access-Control-Request-Method": "POST",
            },
        )
    assert "access-control-allow-origin" not in response.headers


def test_unknown_route_uses_openai_error_format(app: FastAPI) -> None:
    with TestClient(app) as client:
        response = client.get("/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "not_found"
    assert body["error"]["param"] is None
    assert body["error"]["message"]
    assert response.headers["X-Request-ID"]


def test_error_body_shape() -> None:
    body = error_body(
        message="bad", error_type="invalid_request_error", code="bad_request", param="q"
    )
    assert body == {
        "error": {
            "message": "bad",
            "type": "invalid_request_error",
            "param": "q",
            "code": "bad_request",
        }
    }


def test_api_error_handler(app: FastAPI) -> None:
    @app.get("/_test/api-error")
    async def api_error() -> None:
        raise APIError(
            "bad key", status_code=401, error_type="authentication_error", code="invalid_api_key"
        )

    with TestClient(app) as client:
        response = client.get("/_test/api-error")
    assert response.status_code == 401
    assert response.json()["error"] == {
        "message": "bad key",
        "type": "authentication_error",
        "param": None,
        "code": "invalid_api_key",
    }


def test_unhandled_error_handler_returns_generic_message(app: FastAPI) -> None:
    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("SECRET-INTERNAL-DETAIL")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/boom")
    assert response.status_code == 500
    assert "SECRET-INTERNAL-DETAIL" not in response.text
    assert response.json()["error"]["code"] == "internal_error"


async def test_validation_error_handler_shape() -> None:
    from fastapi.exceptions import RequestValidationError

    exc = RequestValidationError(
        errors=[
            {
                "type": "missing",
                "loc": ("body", "model"),
                "msg": "Field required",
                "input": {},
            }
        ]
    )
    from starlette.requests import Request

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    )
    response = await validation_error_handler(request, exc)
    assert response.status_code == 422
    assert response.body
