"""OpenAI-compatible ``POST /v1/chat/completions``.

Flow (in order)
---------------
1. **Size cap** — ``Content-Length`` *and* the buffered body must fit within
   ``settings.max_request_bytes`` (413 ``request_too_large``).
2. **Schema + allowlist** — validate the chat shape, require the ``model`` to
   be an ENABLED ``model_pricing`` row (404 ``model_not_found``), and bound
   ``max_tokens`` by ``settings.max_output_tokens`` (422
   ``max_tokens_exceeded``).
3. **Rate limit** — sliding-window request budget then an in-flight
   concurrency slot (429 ``rate_limit_exceeded`` + ``Retry-After``).
4. **Reservation** — ``max(1, reservation_estimate(...))`` held via
   ``CreditService.reserve`` using the sanitized gateway request id. The hold
   is committed in its own session BEFORE the upstream await, so no DB lock
   is held across the network. A replay of a finished request id is a 409
   ``idempotency_conflict``; insufficient funds is the service's 402.
5. **Upstream** — buffered call or SSE stream; the output budget defaults to
   ``settings.max_output_tokens`` so the hold is always an upper bound.
6. **Settlement** — actual usage drives ``actual_cost`` (clamped to the
   reserved amount). Settlement/usage always run in fresh
   ``database_state.session_factory()`` sessions so a streaming body can
   outlive the request-scoped ``get_db`` session.

Failures before any token refund the FULL hold (``upstream_error``); failures
after partial output settle only what was produced (``partial``). Client
disconnects (``CancelledError``/``GeneratorExit``) settle the tokens actually
produced and always release the concurrency slot.

Prompt/completion content is never logged or persisted.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import httpx
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.auth import get_api_key_principal, request_settings, require_scope
from app.core.errors import (
    APIError,
    IdempotencyConflictError,
    RateLimitError,
    error_body,
)
from app.core.logging import get_logger
from app.core.rate_limit import RateLimiter
from app.db.session import database_state
from app.schemas.v1 import ChatCompletionRequest
from app.services.api_key_service import APIKeyPrincipal
from app.services.credit_service import CreditService
from app.services.llm_client import (
    LiteLLMClient,
    UpstreamClientError,
    UpstreamTimeoutError,
)
from app.services.model_pricing import (
    ModelPricingRepository,
    get_model_pricing_repository,
)
from app.services.pricing import (
    ModelPricingRow,
    actual_cost,
    estimate_prompt_tokens,
    reservation_estimate,
)
from app.services.usage import UsageRecord, record_usage_event


class _CommonKwargs(TypedDict):
    """Shared keyword arguments for stream and non-stream handlers."""

    llm: LiteLLMClient
    billing: ChatBilling
    principal: APIKeyPrincipal
    request_id: str
    model: str
    provider: str
    reservation_id: str
    reserved_amount: int
    pricing: ModelPricingRow
    prompt_estimate: int
    started: float


logger = get_logger(__name__)

router = APIRouter(tags=["chat"])

_MAX_REQUEST_ID_LENGTH = 128


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sanitise_request_id(value: str | None) -> str:
    if not value:
        return str(uuid.uuid4())
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_REQUEST_ID_LENGTH:
        return str(uuid.uuid4())
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        return str(uuid.uuid4())
    return candidate


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _request_too_large(cap: int) -> APIError:
    return APIError(
        f"Request body exceeds the {cap}-byte limit",
        status_code=413,
        error_type="invalid_request_error",
        code="request_too_large",
    )


def _model_not_found(model: str) -> APIError:
    return APIError(
        f"The model '{model}' does not exist",
        status_code=404,
        error_type="invalid_request_error",
        code="model_not_found",
        param="model",
    )


def _max_tokens_exceeded(value: int, limit: int) -> APIError:
    return APIError(
        f"max_tokens must be <= {limit} (received {value})",
        status_code=422,
        error_type="invalid_request_error",
        code="max_tokens_exceeded",
        param="max_tokens",
    )


def _invalid_request_from(exc: ValidationError) -> APIError:
    errors = exc.errors()
    loc = ".".join(str(part) for part in errors[0].get("loc", ())) if errors else ""
    return APIError(
        f"Invalid request: {loc}" if loc else "Invalid request",
        status_code=422,
        error_type="invalid_request_error",
        code="invalid_request",
        param=loc or None,
    )


def _error_response(exc: APIError, *, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(status_code=exc.status_code, content=exc.as_body(), headers=headers)


def _usage_from_body(body: Any) -> dict[str, int] | None:
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion}


def _char_tokens(length: int) -> int:
    """Documented fallback: ``ceil(len/4)`` when upstream omits usage."""
    return max(0, (length + 3) // 4)


def _completion_text_length(body: Any) -> int:
    total = 0
    if isinstance(body, dict):
        for choice in body.get("choices") or []:
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    total += len(content)
    return total


def _delta_text_length(event: Any) -> int:
    total = 0
    if isinstance(event, dict):
        for choice in event.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    total += len(content)
    return total


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------


class ChatBilling:
    """Fresh-session persistence for a single chat request.

    A route-scoped ``get_db`` session is closed once a streaming response
    starts, so reservation/refund/settlement/usage all open their own short
    ``session_factory`` session. Both the factory and the ``CreditService``
    are injectable so unit tests can drive the full flow with in-memory fakes.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        credit_service: CreditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._credit = credit_service or CreditService()

    def _require_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database has not been initialised")
        return self._session_factory

    async def reserve(
        self,
        principal: APIKeyPrincipal,
        *,
        amount: int,
        request_id: str,
        ttl_seconds: int,
    ) -> Any:
        factory = self._require_factory()
        async with factory() as session:
            try:
                reservation = await self._credit.reserve(
                    session,
                    user_id=uuid.UUID(principal.user_id),
                    amount_microcredits=amount,
                    request_id=request_id,
                    api_key_id=uuid.UUID(principal.api_key_id),
                    expires_in_seconds=ttl_seconds,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        if reservation.status != "open":
            raise IdempotencyConflictError(
                "request_id has already been settled or released; "
                "issue a new request id for a fresh completion"
            )
        return reservation

    async def settle(
        self, reservation_id: str, *, cost: int, request_id: str, reserved_amount: int
    ) -> int:
        # Never charge more than was held; a larger real cost is capped and
        # logged rather than violating the reservation invariant.
        charged = max(0, min(cost, reserved_amount))
        if cost > reserved_amount:
            logger.warning(
                "settlement_capped",
                request_id=request_id,
                charged=charged,
                actual=cost,
            )
        factory = self._require_factory()
        async with factory() as session:
            try:
                await self._credit.settle(
                    session,
                    reservation_id=uuid.UUID(reservation_id),
                    cost_microcredits=charged,
                    request_id=request_id,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return charged

    async def refund(self, reservation_id: str, *, reason: str, request_id: str) -> None:
        factory = self._require_factory()
        async with factory() as session:
            try:
                await self._credit.refund(
                    session,
                    reservation_id=uuid.UUID(reservation_id),
                    reason=reason,
                    request_id=request_id,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def record_usage(self, record: UsageRecord) -> None:
        factory = self._require_factory()
        async with factory() as session:
            try:
                await record_usage_event(session, record)
                await session.commit()
            except Exception:
                await session.rollback()
                raise


def get_chat_billing() -> ChatBilling:
    """Default billing collaborator (overridable in tests)."""
    return ChatBilling(database_state.session_factory)


def get_rate_limiter(request: Request) -> RateLimiter:
    settings: Settings = request_settings(request)
    return RateLimiter(
        getattr(request.app.state, "redis", None),
        requests_per_minute=settings.rate_limit_requests_per_minute,
        max_concurrent=settings.rate_limit_max_concurrent,
    )


def get_litellm_client(request: Request) -> LiteLLMClient:
    settings: Settings = request_settings(request)
    shared = getattr(request.app.state, "chat_client", None)
    if shared is None:
        shared = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_read_timeout, connect=settings.http_connect_timeout)
        )
    return LiteLLMClient(
        shared,
        base_url=settings.litellm_base_url,
        master_key=settings.litellm_master_key,
    )


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    principal: Annotated[APIKeyPrincipal, Depends(get_api_key_principal)],
    repository: Annotated[ModelPricingRepository, Depends(get_model_pricing_repository)],
    billing: Annotated[ChatBilling, Depends(get_chat_billing)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    llm: Annotated[LiteLLMClient, Depends(get_litellm_client)],
    content_length: Annotated[int | None, Header(alias="Content-Length")] = None,
) -> Any:
    require_scope(principal, "chat:completions")
    settings: Settings = request_settings(request)

    # --- 1. size cap -----------------------------------------------------
    cap = settings.max_request_bytes
    if content_length is not None and content_length > cap:
        raise _request_too_large(cap)
    raw_body = await request.body()
    if len(raw_body) > cap:
        raise _request_too_large(cap)

    # --- 2. schema + model allowlist + max_tokens ------------------------
    try:
        parsed = json.loads(raw_body) if raw_body else {}
    except ValueError:
        raise APIError(
            "Invalid request: body must be valid JSON",
            status_code=422,
            code="invalid_request",
        ) from None
    try:
        payload = ChatCompletionRequest.model_validate(parsed)
    except ValidationError as exc:
        raise _invalid_request_from(exc) from None

    pricing_row = await repository.get_enabled(payload.model)
    if pricing_row is None:
        raise _model_not_found(payload.model)
    if payload.max_tokens is not None and payload.max_tokens > settings.max_output_tokens:
        raise _max_tokens_exceeded(payload.max_tokens, settings.max_output_tokens)

    pricing = ModelPricingRow(
        input_price_per_1k=int(pricing_row.input_price_per_1k),
        output_price_per_1k=int(pricing_row.output_price_per_1k),
    )

    # --- 3. rate limit ---------------------------------------------------
    try:
        await limiter.check_request_rate(principal.api_key_id)
    except RateLimitError as rate_error:
        return _error_response(rate_error, retry_after=rate_error.retry_after)
    acquired = await limiter.acquire_concurrency(principal.api_key_id)
    if not acquired:
        concurrency_error = RateLimitError(
            "Too many concurrent requests", code="rate_limit_exceeded", retry_after=1
        )
        return _error_response(concurrency_error, retry_after=1)

    release_here = True
    try:
        # --- 4. reservation (commit before upstream) ---------------------
        request_id = _sanitise_request_id(getattr(request.state, "request_id", None))
        prompt_estimate = estimate_prompt_tokens(payload.messages)
        output_budget = (
            payload.max_tokens if payload.max_tokens is not None else settings.max_output_tokens
        )
        amount = max(1, reservation_estimate(prompt_estimate, output_budget, pricing))
        reservation = await billing.reserve(
            principal,
            amount=amount,
            request_id=request_id,
            ttl_seconds=settings.reservation_ttl_seconds,
        )
        reserved_amount = int(reservation.amount_microcredits)
        reservation_id = str(reservation.id)

        forwarded: dict[str, Any] = payload.model_dump(exclude_none=True)
        if payload.stream:
            forwarded["stream_options"] = {"include_usage": True}

        started = time.perf_counter()
        provider = pricing_row.provider
        common: _CommonKwargs = {
            "llm": llm,
            "billing": billing,
            "principal": principal,
            "request_id": request_id,
            "model": payload.model,
            "provider": provider,
            "reservation_id": reservation_id,
            "reserved_amount": reserved_amount,
            "pricing": pricing,
            "prompt_estimate": prompt_estimate,
            "started": started,
        }

        # --- 5+6. upstream + settlement ---------------------------------
        if payload.stream:
            job = _StreamJob(
                payload=forwarded,
                limiter=limiter,
                api_key_id=principal.api_key_id,
                **common,
            )
            # The generator owns the concurrency slot from here on.
            release_here = False
            return StreamingResponse(_stream_events(job), media_type="text/event-stream")
        return await _run_non_stream(payload=forwarded, **common)
    finally:
        if release_here:
            await limiter.release_concurrency(principal.api_key_id)


# ---------------------------------------------------------------------------
# non-stream
# ---------------------------------------------------------------------------


async def _run_non_stream(
    *,
    llm: LiteLLMClient,
    billing: ChatBilling,
    principal: APIKeyPrincipal,
    request_id: str,
    model: str,
    provider: str,
    reservation_id: str,
    reserved_amount: int,
    pricing: ModelPricingRow,
    prompt_estimate: int,
    started: float,
    payload: dict[str, Any],
) -> JSONResponse:
    try:
        body = await llm.chat_completion(payload, request_id=request_id)
    except UpstreamClientError as exc:
        await _refund_and_record(
            billing,
            principal=principal,
            request_id=request_id,
            model=model,
            provider=provider,
            reservation_id=reservation_id,
            started=started,
            error_code=getattr(exc, "code", "upstream_error"),
        )
        return JSONResponse(status_code=exc.status_code, content=exc.body)
    except (UpstreamTimeoutError, APIError) as exc:
        await _refund_and_record(
            billing,
            principal=principal,
            request_id=request_id,
            model=model,
            provider=provider,
            reservation_id=reservation_id,
            started=started,
            error_code=getattr(exc, "code", "upstream_error"),
        )
        return _error_response(exc)

    usage = _usage_from_body(body)
    if usage is None:
        usage = {
            "prompt_tokens": prompt_estimate,
            "completion_tokens": _char_tokens(_completion_text_length(body)),
        }
    cost = actual_cost(usage["prompt_tokens"], usage["completion_tokens"], pricing)
    try:
        charged = await billing.settle(
            reservation_id,
            cost=cost,
            request_id=request_id,
            reserved_amount=reserved_amount,
        )
        await billing.record_usage(
            UsageRecord(
                user_id=principal.user_id,
                api_key_id=principal.api_key_id,
                request_id=request_id,
                model=model,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                cost_microcredits=charged,
                status="success",
                latency_ms=_elapsed_ms(started),
                upstream_provider=provider,
                total_tokens=usage["prompt_tokens"] + usage["completion_tokens"],
                error_code=None,
            )
        )
    except Exception as exc:
        logger.warning("post_success_billing_failed", error_type=type(exc).__name__)
    return JSONResponse(status_code=200, content=body)


async def _refund_and_record(
    billing: ChatBilling,
    *,
    principal: APIKeyPrincipal,
    request_id: str,
    model: str,
    provider: str,
    reservation_id: str,
    started: float,
    error_code: str | None,
) -> None:
    try:
        await billing.refund(reservation_id, reason="upstream_error", request_id=request_id)
    except Exception as exc:
        logger.warning("refund_failed", error_type=type(exc).__name__)
    try:
        await billing.record_usage(
            UsageRecord(
                user_id=principal.user_id,
                api_key_id=principal.api_key_id,
                request_id=request_id,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                cost_microcredits=0,
                status="error",
                latency_ms=_elapsed_ms(started),
                upstream_provider=provider,
                total_tokens=0,
                error_code=error_code,
            )
        )
    except Exception as exc:
        logger.warning("usage_record_failed", error_type=type(exc).__name__)


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


@dataclass
class _StreamJob:
    """Everything a streaming generator needs; also directly unit-testable."""

    llm: LiteLLMClient
    billing: ChatBilling
    limiter: RateLimiter
    api_key_id: str
    payload: dict[str, Any]
    principal: APIKeyPrincipal
    request_id: str
    model: str
    provider: str
    reservation_id: str
    reserved_amount: int
    pricing: ModelPricingRow
    prompt_estimate: int
    started: float


async def _stream_events(job: _StreamJob) -> AsyncIterator[str]:
    """Proxy SSE lines, settling produced tokens and always releasing the slot."""
    produced = False
    text_length = 0
    final_usage: dict[str, int] | None = None
    completed = False
    try:
        try:
            async with job.llm.stream_chat_completion(
                job.payload, request_id=job.request_id
            ) as lines:
                async for line in lines:
                    if line.startswith("data:"):
                        data = line[len("data:") :].strip()
                        if data and data != "[DONE]":
                            try:
                                event = json.loads(data)
                            except ValueError:
                                event = None
                            if isinstance(event, dict):
                                usage = _usage_from_body(event)
                                if usage is not None:
                                    final_usage = usage
                                delta = _delta_text_length(event)
                                if delta:
                                    text_length += delta
                    if text_length > 0 or final_usage is not None:
                        produced = True
                    yield line + "\n\n"
                completed = True
        except UpstreamClientError as exc:
            if not produced:
                await _refund_and_record(
                    job.billing,
                    principal=job.principal,
                    request_id=job.request_id,
                    model=job.model,
                    provider=job.provider,
                    reservation_id=job.reservation_id,
                    started=job.started,
                    error_code=getattr(exc, "code", "upstream_error"),
                )
                yield f"data: {json.dumps(exc.body)}\n\n"
                yield "data: [DONE]\n\n"
                return
            await _settle_partial(job, text_length=text_length, usage=final_usage)
            yield "data: [DONE]\n\n"
            return
        except (UpstreamTimeoutError, APIError) as exc:
            if not produced:
                await _refund_and_record(
                    job.billing,
                    principal=job.principal,
                    request_id=job.request_id,
                    model=job.model,
                    provider=job.provider,
                    reservation_id=job.reservation_id,
                    started=job.started,
                    error_code=getattr(exc, "code", "upstream_error"),
                )
                envelope = error_body(message=exc.message, error_type=exc.error_type, code=exc.code)
                yield f"data: {json.dumps(envelope)}\n\n"
                yield "data: [DONE]\n\n"
                return
            await _settle_partial(job, text_length=text_length, usage=final_usage)
            yield "data: [DONE]\n\n"
            return
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnect: charge exactly what was produced, then stop.
            await _finalize_disconnect(job, text_length=text_length, usage=final_usage)
            raise

        if completed:
            await _finalize_success(job, text_length=text_length, usage=final_usage)
    finally:
        await job.limiter.release_concurrency(job.api_key_id)


async def _finalize_success(
    job: _StreamJob, *, text_length: int, usage: dict[str, int] | None
) -> None:
    if usage is not None:
        prompt_tokens = usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"]
    else:
        prompt_tokens = job.prompt_estimate
        completion_tokens = _char_tokens(text_length)
    cost = actual_cost(prompt_tokens, completion_tokens, job.pricing)
    try:
        charged = await job.billing.settle(
            job.reservation_id,
            cost=cost,
            request_id=job.request_id,
            reserved_amount=job.reserved_amount,
        )
        await job.billing.record_usage(
            UsageRecord(
                user_id=job.principal.user_id,
                api_key_id=job.principal.api_key_id,
                request_id=job.request_id,
                model=job.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_microcredits=charged,
                status="success",
                latency_ms=_elapsed_ms(job.started),
                upstream_provider=job.provider,
                total_tokens=prompt_tokens + completion_tokens,
                error_code=None,
            )
        )
    except Exception as exc:
        logger.warning("stream_settlement_failed", error_type=type(exc).__name__)


async def _settle_partial(
    job: _StreamJob, *, text_length: int, usage: dict[str, int] | None
) -> None:
    completion_tokens = (
        usage["completion_tokens"] if usage is not None else _char_tokens(text_length)
    )
    prompt_tokens = usage["prompt_tokens"] if usage is not None else job.prompt_estimate
    cost = actual_cost(prompt_tokens, completion_tokens, job.pricing)
    try:
        charged = await job.billing.settle(
            job.reservation_id,
            cost=cost,
            request_id=job.request_id,
            reserved_amount=job.reserved_amount,
        )
        await job.billing.record_usage(
            UsageRecord(
                user_id=job.principal.user_id,
                api_key_id=job.principal.api_key_id,
                request_id=job.request_id,
                model=job.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_microcredits=charged,
                status="partial",
                latency_ms=_elapsed_ms(job.started),
                upstream_provider=job.provider,
                total_tokens=prompt_tokens + completion_tokens,
                error_code="upstream_error",
            )
        )
    except Exception as exc:
        logger.warning("stream_partial_settlement_failed", error_type=type(exc).__name__)


async def _finalize_disconnect(
    job: _StreamJob, *, text_length: int, usage: dict[str, int] | None
) -> None:
    """On disconnect charge only what was produced; refund if nothing was."""
    completion_tokens = (
        usage["completion_tokens"] if usage is not None else _char_tokens(text_length)
    )
    prompt_tokens = usage["prompt_tokens"] if usage is not None else 0
    if completion_tokens <= 0 and prompt_tokens <= 0:
        try:
            await job.billing.refund(
                job.reservation_id, reason="client_disconnect", request_id=job.request_id
            )
        except Exception as exc:
            logger.warning("disconnect_refund_failed", error_type=type(exc).__name__)
        # Record the disconnect even though nothing billable was produced.
        try:
            await job.billing.record_usage(
                UsageRecord(
                    user_id=job.principal.user_id,
                    api_key_id=job.principal.api_key_id,
                    request_id=job.request_id,
                    model=job.model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_microcredits=0,
                    status="error",
                    latency_ms=_elapsed_ms(job.started),
                    upstream_provider=job.provider,
                    total_tokens=0,
                    error_code="client_disconnect",
                )
            )
        except Exception as exc:
            logger.warning("disconnect_usage_failed", error_type=type(exc).__name__)
        return
    cost = actual_cost(prompt_tokens, completion_tokens, job.pricing)
    try:
        charged = await job.billing.settle(
            job.reservation_id,
            cost=cost,
            request_id=job.request_id,
            reserved_amount=job.reserved_amount,
        )
        await job.billing.record_usage(
            UsageRecord(
                user_id=job.principal.user_id,
                api_key_id=job.principal.api_key_id,
                request_id=job.request_id,
                model=job.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_microcredits=charged,
                status="partial",
                latency_ms=_elapsed_ms(job.started),
                upstream_provider=job.provider,
                total_tokens=prompt_tokens + completion_tokens,
                error_code="client_disconnect",
            )
        )
    except Exception as exc:
        logger.warning("disconnect_settlement_failed", error_type=type(exc).__name__)
