"""Request ID propagation and dashboard-only CORS middleware."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

REQUEST_ID_HEADER = "X-Request-ID"
CONTEXT_REQUEST_ID = "request_id"
_MAX_REQUEST_ID_LENGTH = 128


def _sanitise_request_id(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_REQUEST_ID_LENGTH:
        return None
    # Reject control characters and whitespace to keep logs and headers safe.
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        return None
    return candidate


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a request ID to every request, response, and log context."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = _sanitise_request_id(request.headers.get(REQUEST_ID_HEADER))
        request_id = incoming or str(uuid.uuid4())
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**{CONTEXT_REQUEST_ID: request_id})
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class DashboardCORSMiddleware:
    """Apply CORS only to dashboard routes, never to the public ``/v1`` API.

    The OpenAI-compatible API is consumed server-side or by API-key clients;
    browser credentials belong only to the dashboard origin configured as
    ``app_url``.
    """

    def __init__(self, app: ASGIApp, allow_origins: list[str]) -> None:
        self.app = app
        self._cors = CORSMiddleware(
            app,
            allow_origins=allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
            max_age=600,
        )

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/v1" or path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        await self._cors(scope, receive, send)
