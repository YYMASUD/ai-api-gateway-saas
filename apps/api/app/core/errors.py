"""OpenAI-compatible error responses and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class APIError(Exception):
    """Base class for gateway errors rendered in OpenAI's error format."""

    status_code: int = 400
    error_type: str = "invalid_request_error"
    code: str | None = None
    param: str | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if error_type is not None:
            self.error_type = error_type
        if code is not None:
            self.code = code
        if param is not None:
            self.param = param

    def as_body(self) -> dict[str, Any]:
        return error_body(
            message=self.message,
            error_type=self.error_type,
            code=self.code,
            param=self.param,
        )


class AuthenticationError(APIError):
    status_code = 401
    error_type = "authentication_error"
    code = "invalid_api_key"


class PermissionDeniedError(APIError):
    status_code = 403
    error_type = "permission_error"
    code = "forbidden"


class NotFoundError(APIError):
    status_code = 404
    error_type = "invalid_request_error"
    code = "not_found"


class RateLimitError(APIError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limit_exceeded"


class UpstreamError(APIError):
    status_code = 502
    error_type = "api_error"
    code = "upstream_error"


def error_body(
    *,
    message: str,
    error_type: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    """Return the OpenAI-compatible error envelope."""
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def _status_to_type(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "invalid_request_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "api_error"
    return "invalid_request_error"


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, APIError)
    return JSONResponse(status_code=exc.status_code, content=exc.as_body())


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    first = errors[0] if errors else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = "Invalid request"
    if location:
        message = f"Invalid request: {location}"
    return JSONResponse(
        status_code=422,
        content=error_body(message=message, code="invalid_request", param=location or None),
    )


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status_code = exc.status_code
    detail = exc.detail
    message = detail if isinstance(detail, str) else _status_to_message(status_code)
    if status_code >= 500:
        message = "Internal server error"
    return JSONResponse(
        status_code=status_code,
        content=error_body(
            message=message,
            error_type=_status_to_type(status_code),
            code=_status_code_to_code(status_code),
        ),
        headers=getattr(exc, "headers", None),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        request_id=getattr(request.state, "request_id", None),
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content=error_body(
            message="Internal server error",
            error_type="api_error",
            code="internal_error",
        ),
    )


def _status_to_message(status_code: int) -> str:
    if status_code == 401:
        return "Authentication required"
    if status_code == 403:
        return "Permission denied"
    if status_code == 404:
        return "Not found"
    if status_code == 405:
        return "Method not allowed"
    if status_code == 429:
        return "Rate limit exceeded"
    if status_code >= 500:
        return "Internal server error"
    return "Request failed"


def _status_code_to_code(status_code: int) -> str | None:
    if status_code == 401:
        return "invalid_api_key"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 405:
        return "method_not_allowed"
    if status_code == 429:
        return "rate_limit_exceeded"
    if status_code >= 500:
        return "internal_error"
    return None


def register_exception_handlers(app: Any) -> None:
    """Attach OpenAI-compatible handlers to a FastAPI application."""
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
