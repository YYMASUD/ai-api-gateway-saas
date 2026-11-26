"""Unit tests for the stateless LiteLLM httpx client (MockTransport only)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.core.errors import UpstreamError
from app.core.logging import configure_logging
from app.services.llm_client import (
    LiteLLMClient,
    UpstreamClientError,
    UpstreamTimeoutError,
)

PROMPT_SECRET = "PROMPT-CONTENT-MUST-NOT-BE-LOGGED-8f31"
COMPLETION_SECRET = "COMPLETION-CONTENT-MUST-NOT-BE-LOGGED-22ab"
MASTER_KEY_SECRET = "MASTER-KEY-MUST-NOT-BE-LOGGED-cd77"


def _client(settings: Settings, handler: Any) -> tuple[LiteLLMClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        LiteLLMClient(
            http,
            base_url=settings.litellm_base_url,
            master_key=MASTER_KEY_SECRET,
        ),
        http,
    )


async def test_chat_completion_success_propagates_headers(settings: Settings) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("Authorization")
        seen["request_id"] = request.headers.get("X-Request-ID")
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    client, http = _client(settings, handler)
    async with http:
        body = await client.chat_completion(
            {"model": "gateway-default", "messages": [{"role": "user", "content": "hi"}]},
            request_id="req-42",
        )
    assert seen["url"] == f"{settings.litellm_base_url}/v1/chat/completions"
    assert seen["authorization"] == f"Bearer {MASTER_KEY_SECRET}"
    assert seen["request_id"] == "req-42"
    assert body["id"] == "chatcmpl-1"


async def test_chat_completion_maps_5xx_to_upstream_error(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "upstream down"}})

    client, http = _client(settings, handler)
    async with http:
        with pytest.raises(UpstreamError) as exc_info:
            await client.chat_completion({"model": "m", "messages": []}, request_id=None)
    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "upstream_error"


async def test_chat_completion_maps_timeout_to_504(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client, http = _client(settings, handler)
    async with http:
        with pytest.raises(UpstreamTimeoutError) as exc_info:
            await client.chat_completion({"model": "m", "messages": []}, request_id=None)
    assert exc_info.value.status_code == 504
    assert exc_info.value.code == "upstream_timeout"


async def test_chat_completion_maps_connection_error_to_504(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client, http = _client(settings, handler)
    async with http:
        with pytest.raises(UpstreamTimeoutError):
            await client.chat_completion({"model": "m", "messages": []}, request_id=None)


async def test_chat_completion_4xx_returns_relayable_body(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request", "code": "bad"}})

    client, http = _client(settings, handler)
    async with http:
        with pytest.raises(UpstreamClientError) as exc_info:
            await client.chat_completion({"model": "m", "messages": []}, request_id=None)
    assert exc_info.value.status_code == 400
    assert exc_info.value.body["error"]["code"] == "bad"


async def test_stream_yields_raw_lines(settings: Settings) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request_id"] = request.headers.get("X-Request-ID")
        content = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":2,"completion_tokens":1},"choices":[]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    client, http = _client(settings, handler)
    async with http:
        async with client.stream_chat_completion(
            {"model": "m", "messages": [], "stream": True}, request_id="req-stream"
        ) as lines:
            rendered = [line async for line in lines]
    assert seen["request_id"] == "req-stream"
    assert len(rendered) >= 3
    assert any("DONE" in line for line in rendered)


async def test_stream_maps_5xx_and_timeout(settings: Settings) -> None:
    def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client, http = _client(settings, server_error)
    async with http:
        with pytest.raises(UpstreamError) as exc_info:
            async with client.stream_chat_completion({}, request_id=None):
                pass
    assert exc_info.value.status_code == 502

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client, http = _client(settings, timeout)
    async with http:
        with pytest.raises(UpstreamTimeoutError):
            async with client.stream_chat_completion({}, request_id=None):
                pass


async def test_no_content_or_key_in_logs(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": COMPLETION_SECRET}}]},
        )

    client, http = _client(settings, handler)
    async with http:
        await client.chat_completion(
            {
                "model": "m",
                "messages": [{"role": "user", "content": PROMPT_SECRET}],
            },
            request_id="req-log",
        )
    captured = capsys.readouterr().out
    assert seen["authorization"] == f"Bearer {MASTER_KEY_SECRET}"
    assert PROMPT_SECRET not in captured
    assert COMPLETION_SECRET not in captured
    assert MASTER_KEY_SECRET not in captured
