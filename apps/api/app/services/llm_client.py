"""Stateless LiteLLM HTTP client for the ``/v1`` surface.

The gateway forwards chat completion payloads to a LiteLLM proxy through a
shared ``httpx.AsyncClient``. Billing is authoritative in this FastAPI
service (LiteLLM is stateless), so this client only transports bytes.

Privacy: prompts, completions, and the master key are NEVER logged. The
structured-log redactor would mask them anyway, but this module deliberately
keeps them out of every log record; only request ids, status codes, and
exception class names are logged.

Error mapping
-------------
* HTTP 5xx → ``UpstreamError`` (502, ``upstream_error``)
* timeouts / connection failures → ``UpstreamTimeoutError`` (504,
  ``upstream_timeout``)
* HTTP 4xx → ``UpstreamClientError`` carrying the upstream body and status,
  so the route can relay the upstream's own OpenAI error envelope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.core.errors import APIError, UpstreamError
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "LiteLLMClient",
    "UpstreamClientError",
    "UpstreamTimeoutError",
]


class UpstreamTimeoutError(APIError):
    """Upstream did not respond within the configured timeout."""

    status_code = 504
    error_type = "api_error"
    code = "upstream_timeout"


class UpstreamClientError(APIError):
    """Upstream returned a 4xx; relay its envelope with the original status."""

    def __init__(self, body: dict[str, Any], status_code: int) -> None:
        super().__init__(
            "Upstream rejected the request",
            status_code=status_code,
            error_type="invalid_request_error",
            code="upstream_client_error",
        )
        self.body = body


def _upstream_5xx(status_code: int) -> UpstreamError:
    return UpstreamError(f"Upstream returned status {status_code}", code="upstream_error")


class LiteLLMClient:
    """Thin httpx wrapper that talks to the LiteLLM proxy."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        master_key: str,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._master_key = master_key

    def _headers(self, request_id: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._master_key}"}
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _url(self) -> str:
        return f"{self._base_url}/v1/chat/completions"

    async def chat_completion(
        self, payload: dict[str, Any], *, request_id: str | None
    ) -> dict[str, Any]:
        """POST a buffered completion and return the parsed JSON body."""
        try:
            response = await self._client.post(
                self._url(), json=payload, headers=self._headers(request_id)
            )
        except httpx.TimeoutException as exc:
            logger.warning("litellm_timeout", error_type=type(exc).__name__)
            raise UpstreamTimeoutError("Upstream request timed out") from None
        except httpx.HTTPError as exc:
            logger.warning("litellm_connection_error", error_type=type(exc).__name__)
            raise UpstreamTimeoutError("Upstream is unreachable") from None

        if response.status_code >= 500:
            logger.warning("litellm_upstream_5xx", status=response.status_code)
            raise _upstream_5xx(response.status_code)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"error": {"message": "Upstream rejected the request"}}
            raise UpstreamClientError(body, response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError("Upstream returned an invalid response") from exc
        if not isinstance(data, dict):
            raise UpstreamError("Upstream returned an unexpected response")
        return data

    @asynccontextmanager
    async def stream_chat_completion(
        self, payload: dict[str, Any], *, request_id: str | None
    ) -> AsyncIterator[AsyncIterator[str]]:
        """Open an SSE stream and yield decoded lines without buffering.

        Yields an async iterator that produces each line exactly as received
        (newline stripped). Errors before the first line raise the same
        mapped exceptions as ``chat_completion``.
        """
        try:
            async with self._client.stream(
                "POST",
                self._url(),
                json=payload,
                headers=self._headers(request_id),
            ) as response:
                if response.status_code >= 500:
                    logger.warning("litellm_upstream_5xx", status=response.status_code)
                    raise _upstream_5xx(response.status_code)
                if response.status_code >= 400:
                    await response.aread()
                    try:
                        body = response.json()
                    except ValueError:
                        body = {"error": {"message": "Upstream rejected the request"}}
                    raise UpstreamClientError(body, response.status_code)

                async def _lines() -> AsyncIterator[str]:
                    async for line in response.aiter_lines():
                        yield line

                yield _lines()
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.warning("litellm_stream_error", error_type=type(exc).__name__)
            if isinstance(exc, httpx.TimeoutException):
                raise UpstreamTimeoutError("Upstream stream timed out") from None
            raise UpstreamTimeoutError("Upstream stream failed") from None
