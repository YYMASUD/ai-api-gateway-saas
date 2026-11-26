"""Tests for POST /v1/chat/completions: auth, validation, billing, streaming.

No external services: the pricing repository, credit repository, rate limiter,
and LiteLLM client are all in-memory doubles. Streaming is exercised both via
TestClient and by driving the module-level generator directly (to inject a
client disconnect deterministically).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.auth import get_api_key_principal, get_api_key_repository
from app.core.errors import RateLimitError, UpstreamError
from app.core.logging import configure_logging
from app.db.models import CreditReservation, ModelPricing
from app.routers import v1_chat
from app.services.api_key_service import APIKeyPrincipal
from app.services.credit_service import CreditService
from app.services.llm_client import UpstreamClientError, UpstreamTimeoutError
from app.services.model_pricing import get_model_pricing_repository
from app.services.pricing import ModelPricingRow
from tests.fakes import FakeAPIKeyRepository, make_user
from tests.test_credit_service import FakeCreditRepository

PROMPT_SECRET = "PROMPT-SECRET-CONTENT-4a1f"
COMPLETION_SECRET = "COMPLETION-SECRET-CONTENT-9c2e"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _pricing_row(
    model_id: str = "gateway-default",
    *,
    input_price: int = 0,
    output_price: int = 0,
    enabled: bool = True,
) -> ModelPricing:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return ModelPricing(
        id=uuid.uuid4(),
        model_id=model_id,
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
        enabled=enabled,
        provider="litellm",
        metadata_json={},
        created_at=now,
        updated_at=now,
    )


class FakePricingRepository:
    def __init__(self, rows: list[ModelPricing]) -> None:
        self.rows = rows

    async def list_enabled(self) -> list[ModelPricing]:
        return [row for row in self.rows if row.enabled]

    async def get_enabled(self, model_id: str) -> ModelPricing | None:
        return next((row for row in self.rows if row.model_id == model_id and row.enabled), None)


class _BillingSession:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


@asynccontextmanager
async def _billing_factory() -> AsyncIterator[_BillingSession]:
    yield _BillingSession()


class CapturingBilling(v1_chat.ChatBilling):
    def __init__(self, credit_service: CreditService) -> None:
        super().__init__(_billing_factory, credit_service)  # type: ignore[arg-type]
        self.usage: list[Any] = []

    async def record_usage(self, record: Any) -> None:
        self.usage.append(record)


class StubLimiter:
    def __init__(self, *, deny_rate: bool = False, deny_concurrency: bool = False) -> None:
        self.deny_rate = deny_rate
        self.deny_concurrency = deny_concurrency
        self.acquired = 0
        self.released = 0

    async def check_request_rate(self, api_key_id: str) -> None:
        if self.deny_rate:
            raise RateLimitError(
                "Request rate limit exceeded", code="rate_limit_exceeded", retry_after=7
            )

    async def acquire_concurrency(self, api_key_id: str) -> bool:
        if self.deny_concurrency:
            return False
        self.acquired += 1
        return True

    async def release_concurrency(self, api_key_id: str) -> None:
        self.released += 1


class FakeLLM:
    def __init__(
        self,
        *,
        body: dict[str, Any] | None = None,
        exc: Exception | None = None,
        sse: list[Any] | None = None,
    ) -> None:
        self.body = body
        self.exc = exc
        self.sse = sse or []
        self.calls: list[tuple[dict[str, Any], str | None]] = []

    async def chat_completion(
        self, payload: dict[str, Any], *, request_id: str | None
    ) -> dict[str, Any]:
        self.calls.append((payload, request_id))
        if self.exc is not None:
            raise self.exc
        assert self.body is not None
        return self.body

    @asynccontextmanager
    async def stream_chat_completion(
        self, payload: dict[str, Any], *, request_id: str | None
    ) -> AsyncIterator[AsyncIterator[str]]:
        self.calls.append((payload, request_id))
        if self.exc is not None:
            raise self.exc

        async def _lines() -> AsyncIterator[str]:
            for item in self.sse:
                if isinstance(item, BaseException):
                    raise item
                yield item

        yield _lines()


def _ok_body(prompt: int = 5, completion: int = 3) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gateway-default",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": COMPLETION_SECRET},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


class Harness:
    def __init__(
        self,
        client: TestClient,
        *,
        billing: CapturingBilling,
        credit_repo: FakeCreditRepository,
        limiter: StubLimiter,
        llm: FakeLLM,
        principal: APIKeyPrincipal,
        user_id: uuid.UUID,
    ) -> None:
        self.client = client
        self.billing = billing
        self.credit_repo = credit_repo
        self.limiter = limiter
        self.llm = llm
        self.principal = principal
        self.user_id = user_id


def _principal(*scopes: str) -> APIKeyPrincipal:
    return APIKeyPrincipal(
        user_id=str(uuid.uuid4()),
        api_key_id=str(uuid.uuid4()),
        scopes=scopes,
    )


def _harness(
    app: FastAPI,
    *,
    scopes: tuple[str, ...] = ("chat:completions", "models:read"),
    rows: list[ModelPricing] | None = None,
    balance: int = 1_000_000,
    llm: FakeLLM | None = None,
    limiter: StubLimiter | None = None,
) -> Harness:
    user = make_user()
    repo = FakeCreditRepository()
    repo.register_user(user)
    repo.seed_balance(user.id, balance)
    principal = APIKeyPrincipal(
        user_id=str(user.id),
        api_key_id=str(uuid.uuid4()),
        scopes=scopes,
    )
    billing = CapturingBilling(CreditService(repo))
    stub_limiter = limiter or StubLimiter()
    fake_llm = llm or FakeLLM(body=_ok_body())
    app.dependency_overrides[get_api_key_principal] = lambda: principal
    app.dependency_overrides[get_model_pricing_repository] = lambda: FakePricingRepository(
        rows if rows is not None else [_pricing_row()]
    )
    app.dependency_overrides[v1_chat.get_chat_billing] = lambda: billing
    app.dependency_overrides[v1_chat.get_rate_limiter] = lambda: stub_limiter
    app.dependency_overrides[v1_chat.get_litellm_client] = lambda: fake_llm
    return Harness(
        TestClient(app, raise_server_exceptions=False),
        billing=billing,
        credit_repo=repo,
        limiter=stub_limiter,
        llm=fake_llm,
        principal=principal,
        user_id=user.id,
    )


def _body(
    *,
    model: str = "gateway-default",
    content: str = "hello",
    max_tokens: int | None = 10,
    stream: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if stream:
        payload["stream"] = True
    return payload


def _post(harness: Harness, payload: dict[str, Any], *, request_id: str | None = None) -> Any:
    headers = {"X-Request-ID": request_id} if request_id else {}
    return harness.client.post("/v1/chat/completions", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# auth / scope
# ---------------------------------------------------------------------------


def test_chat_requires_api_key(app: FastAPI) -> None:
    app.dependency_overrides[get_api_key_repository] = lambda: FakeAPIKeyRepository()
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/v1/chat/completions", json=_body())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_chat_requires_chat_scope(app: FastAPI) -> None:
    harness = _harness(app, scopes=("models:read",))
    response = _post(harness, _body())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_chat_unknown_model_404(app: FastAPI) -> None:
    harness = _harness(app, rows=[])
    response = _post(harness, _body(model="missing-model"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_chat_disabled_model_404(app: FastAPI) -> None:
    harness = _harness(app, rows=[_pricing_row("gateway-default", enabled=False)])
    response = _post(harness, _body())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_chat_max_tokens_exceeded_422(app: FastAPI, settings: Settings) -> None:
    harness = _harness(app)
    response = _post(harness, _body(max_tokens=settings.max_output_tokens + 1))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "max_tokens_exceeded"
    assert harness.llm.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "gateway-default", "messages": []},
        {"model": "gateway-default"},
        {"model": "", "messages": [{"role": "user", "content": "x"}]},
        {
            "model": "gateway-default",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": 0,
        },
    ],
)
def test_chat_schema_validation_422(app: FastAPI, payload: dict[str, Any]) -> None:
    harness = _harness(app)
    response = harness.client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_chat_invalid_json_422(app: FastAPI) -> None:
    harness = _harness(app)
    response = harness.client.post(
        "/v1/chat/completions",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_chat_request_too_large_413(app: FastAPI, settings: Settings) -> None:
    settings.max_request_bytes = 2048
    harness = _harness(app)
    response = _post(harness, _body(content="x" * 4096))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert harness.llm.calls == []


# ---------------------------------------------------------------------------
# rate limit / credits
# ---------------------------------------------------------------------------


def test_chat_rate_limit_429_sets_retry_after(app: FastAPI) -> None:
    harness = _harness(app, limiter=StubLimiter(deny_rate=True))
    response = _post(harness, _body())
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert harness.llm.calls == []
    assert harness.credit_repo.reservations == {}


def test_chat_concurrency_limit_429(app: FastAPI) -> None:
    harness = _harness(app, limiter=StubLimiter(deny_concurrency=True))
    response = _post(harness, _body())
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert harness.credit_repo.reservations == {}


def test_chat_insufficient_credits_402(app: FastAPI) -> None:
    harness = _harness(app, balance=0)
    response = _post(harness, _body())
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "insufficient_credits"
    assert harness.llm.calls == []


# ---------------------------------------------------------------------------
# non-stream success / failures
# ---------------------------------------------------------------------------


def test_chat_success_settles_and_records_usage(app: FastAPI) -> None:
    harness = _harness(
        app,
        rows=[_pricing_row(input_price=10, output_price=20)],
        balance=1_000_000,
        llm=FakeLLM(body=_ok_body(prompt=5, completion=3)),
    )
    response = _post(harness, _body(content="hello", max_tokens=10), request_id="req-success")

    assert response.status_code == 200
    assert response.json() == _ok_body(prompt=5, completion=3)

    # Forwarded payload and request id are intact; content is passed upstream.
    forwarded, rid = harness.llm.calls[0]
    assert rid == "req-success"
    assert forwarded["messages"][0]["content"] == "hello"

    usage = harness.billing.usage[-1]
    assert (usage.status, usage.prompt_tokens, usage.completion_tokens) == ("success", 5, 3)
    # ceil(5*10/1000)=1 + ceil(3*20/1000)=1
    assert usage.cost_microcredits == 2

    reservation = next(iter(harness.credit_repo.reservations.values()))
    assert reservation.status == "settled"
    assert reservation.cost_microcredits == 2
    assert reservation.request_id == "req-success"

    balance = harness.credit_repo.balances[harness.user_id]
    assert balance.balance == 1_000_000 - 2
    assert balance.reserved == 0
    settlement = [entry for entry in harness.credit_repo.ledgers if entry.type == "settlement"]
    assert settlement and settlement[-1].amount_microcredits == -2

    # No prompt/completion content is persisted in usage records.
    assert PROMPT_SECRET not in repr(usage)
    assert COMPLETION_SECRET not in repr(usage)


def test_chat_success_zero_price_charges_zero(app: FastAPI) -> None:
    harness = _harness(app)
    response = _post(harness, _body())
    assert response.status_code == 200
    assert harness.billing.usage[-1].cost_microcredits == 0
    reservation = next(iter(harness.credit_repo.reservations.values()))
    assert reservation.status == "settled"
    assert reservation.cost_microcredits == 0


def test_chat_success_without_usage_uses_char_fallback(app: FastAPI) -> None:
    body = {
        "id": "x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "abcdefghij"}}],
    }
    harness = _harness(app, llm=FakeLLM(body=body))
    response = _post(harness, _body())
    assert response.status_code == 200
    usage = harness.billing.usage[-1]
    # ceil(10/4) == 3
    assert usage.completion_tokens == 3
    assert usage.status == "success"


def test_chat_upstream_5xx_refunds_full_reservation(app: FastAPI) -> None:
    harness = _harness(
        app, llm=FakeLLM(exc=UpstreamError("upstream exploded", code="upstream_error"))
    )
    response = _post(harness, _body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"

    reservation = next(iter(harness.credit_repo.reservations.values()))
    assert reservation.status == "released"
    balance = harness.credit_repo.balances[harness.user_id]
    assert balance.balance == 1_000_000
    assert balance.reserved == 0
    refunds = [entry for entry in harness.credit_repo.ledgers if entry.type == "refund"]
    assert refunds
    assert harness.billing.usage[-1].status == "error"
    assert harness.billing.usage[-1].cost_microcredits == 0


def test_chat_upstream_timeout_504(app: FastAPI) -> None:
    harness = _harness(app, llm=FakeLLM(exc=UpstreamTimeoutError("timed out")))
    response = _post(harness, _body())
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"
    reservation = next(iter(harness.credit_repo.reservations.values()))
    assert reservation.status == "released"


def test_chat_upstream_4xx_relayed_and_refunded(app: FastAPI) -> None:
    relay = {"error": {"message": "bad", "type": "invalid_request_error", "code": "bad"}}
    harness = _harness(app, llm=FakeLLM(exc=UpstreamClientError(relay, 400)))
    response = _post(harness, _body())
    assert response.status_code == 400
    assert response.json() == relay
    reservation = next(iter(harness.credit_repo.reservations.values()))
    assert reservation.status == "released"


def test_chat_replay_of_settled_request_409(app: FastAPI) -> None:
    harness = _harness(app)
    service = CreditService(harness.credit_repo)
    reservation = harness.credit_repo

    # Pre-settle a request using the same id, amount (1), and api_key_id.
    async def _seed() -> None:
        created = await service.reserve(
            None,
            user_id=harness.user_id,
            amount_microcredits=1,
            request_id="req-replay",
            api_key_id=uuid.UUID(harness.principal.api_key_id),
        )
        await service.settle(
            None,
            reservation_id=created.id,
            cost_microcredits=0,
            request_id="req-replay",
        )

    import asyncio

    asyncio.run(_seed())
    assert reservation.reservations

    response = _post(harness, _body(), request_id="req-replay")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert harness.llm.calls == []


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def _sse_success() -> list[str]:
    return [
        'data: {"choices":[{"delta":{"content":"hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}',
        "data: [DONE]",
    ]


def test_chat_streaming_success_settles(app: FastAPI) -> None:
    harness = _harness(app, llm=FakeLLM(sse=_sse_success()))
    response = _post(harness, _body(stream=True), request_id="req-stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in response.text
    assert "hel" in response.text

    # stream_options was injected so the final chunk carries usage.
    forwarded, rid = harness.llm.calls[0]
    assert forwarded["stream_options"] == {"include_usage": True}
    assert rid == "req-stream"

    usage = harness.billing.usage[-1]
    assert (usage.status, usage.prompt_tokens, usage.completion_tokens) == ("success", 4, 2)
    reservation = next(iter(harness.credit_repo.reservations.values()))
    assert reservation.status == "settled"
    assert harness.limiter.released == 1


def test_chat_streaming_success_without_usage_uses_char_fallback(app: FastAPI) -> None:
    harness = _harness(
        app,
        llm=FakeLLM(
            sse=[
                'data: {"choices":[{"delta":{"content":"abcdefgh"}}]}',
                "data: [DONE]",
            ]
        ),
    )
    response = _post(harness, _body(stream=True))
    assert response.status_code == 200
    usage = harness.billing.usage[-1]
    # ceil(8/4) == 2
    assert usage.completion_tokens == 2
    assert usage.status == "success"


# --- direct generator tests (deterministic disconnects) --------------------


async def _open_job(
    app: FastAPI,
    *,
    sse: list[Any],
    pricing: ModelPricingRow,
    max_tokens: int = 10,
    request_id: str = "req-direct",
) -> tuple[v1_chat._StreamJob, Harness, CreditReservation]:
    harness = _harness(app)
    # Build a dedicated billing/limiter/llm we control for the generator.
    billing = harness.billing
    reservation = await billing.reserve(
        harness.principal,
        amount=16,
        request_id=request_id,
        ttl_seconds=900,
    )
    job = v1_chat._StreamJob(
        llm=FakeLLM(sse=sse),  # type: ignore[arg-type]
        billing=billing,
        limiter=harness.limiter,  # type: ignore[arg-type]
        api_key_id=harness.principal.api_key_id,
        payload={"stream": True},
        principal=harness.principal,
        request_id=request_id,
        model="gateway-default",
        provider="litellm",
        reservation_id=str(reservation.id),
        reserved_amount=reservation.amount_microcredits,
        pricing=pricing,
        prompt_estimate=8,
        started=time.perf_counter(),
    )
    return job, harness, reservation


async def test_streaming_client_disconnect_charges_only_produced_tokens(
    app: FastAPI,
) -> None:
    job, harness, reservation = await _open_job(
        app,
        sse=[
            'data: {"choices":[{"delta":{"content":"hello"}}]}',
            'data: {"choices":[{"delta":{"content":"world"}}]}',
        ],
        pricing=ModelPricingRow(input_price_per_1k=10, output_price_per_1k=20),
    )
    agen = v1_chat._stream_events(job)
    first = await anext(agen)
    assert "hello" in first
    await agen.aclose()  # simulate the client hanging up

    usage = harness.billing.usage[-1]
    # Only the first 5 chars ("hello") were produced: ceil(5/4) == 2.
    assert usage.status == "partial"
    assert usage.completion_tokens == 2
    # ceil(2*20/1000) == 1; prompt tokens are not charged on disconnect.
    assert usage.cost_microcredits == 1
    stored = harness.credit_repo.reservations[reservation.id]
    assert stored.status == "settled"
    assert stored.cost_microcredits == 1
    assert harness.limiter.released == 1


async def test_streaming_disconnect_before_tokens_refunds(app: FastAPI) -> None:
    job, harness, reservation = await _open_job(
        app,
        sse=['data: {"choices":[{"delta":{"role":"assistant"}}]}'],
        pricing=ModelPricingRow(input_price_per_1k=10, output_price_per_1k=20),
    )
    agen = v1_chat._stream_events(job)
    await anext(agen)
    await agen.aclose()

    stored = harness.credit_repo.reservations[reservation.id]
    assert stored.status == "released"
    assert harness.billing.usage[-1].status == "error"
    assert harness.billing.usage[-1].completion_tokens == 0
    assert harness.limiter.released == 1


async def test_streaming_upstream_failure_after_tokens_settles_partial(app: FastAPI) -> None:
    job, harness, reservation = await _open_job(
        app,
        sse=[
            'data: {"choices":[{"delta":{"content":"partial"}}]}',
            UpstreamError("mid-stream failure", code="upstream_error"),
        ],
        pricing=ModelPricingRow(input_price_per_1k=10, output_price_per_1k=20),
    )
    chunks = [chunk async for chunk in v1_chat._stream_events(job)]
    assert any("[DONE]" in chunk for chunk in chunks)

    usage = harness.billing.usage[-1]
    assert usage.status == "partial"
    assert usage.completion_tokens == 2  # ceil(7/4)
    stored = harness.credit_repo.reservations[reservation.id]
    assert stored.status == "settled"
    assert harness.limiter.released == 1


async def test_streaming_upstream_failure_before_tokens_refunds(app: FastAPI) -> None:
    job, harness, reservation = await _open_job(
        app,
        sse=[UpstreamTimeoutError("timed out")],
        pricing=ModelPricingRow(input_price_per_1k=10, output_price_per_1k=20),
    )
    chunks = [chunk async for chunk in v1_chat._stream_events(job)]
    assert any("upstream_timeout" in chunk for chunk in chunks)
    assert any("[DONE]" in chunk for chunk in chunks)

    stored = harness.credit_repo.reservations[reservation.id]
    assert stored.status == "released"
    assert harness.billing.usage[-1].status == "error"
    assert harness.limiter.released == 1


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------


def test_chat_content_never_logged(app: FastAPI, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    body = _ok_body()
    body["choices"][0]["message"]["content"] = COMPLETION_SECRET
    harness = _harness(app, llm=FakeLLM(body=body))
    response = _post(harness, _body(content=PROMPT_SECRET), request_id="req-privacy")
    assert response.status_code == 200
    captured = capsys.readouterr().out
    assert PROMPT_SECRET not in captured
    assert COMPLETION_SECRET not in captured
