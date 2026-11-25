"""Health check endpoints for liveness and readiness probes.

``/healthz`` is dependency-free for orchestrator liveness checks. ``/readyz``
verifies Postgres, Redis, and LiteLLM connectivity and returns 503 when any
dependency is unavailable. Responses never include credentials.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.redis import create_redis_client, ping_redis
from app.db.session import database_state

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

_READINESS_TIMEOUT_SECONDS = 5.0


class DependencyCheck:
    """Result of one dependency health check."""

    __slots__ = ("error", "latency_ms", "name", "ok")

    def __init__(
        self,
        name: str,
        *,
        ok: bool,
        latency_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        self.name = name
        self.ok = ok
        self.latency_ms = latency_ms
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok" if self.ok else "unavailable"}
        if self.latency_ms is not None:
            result["latency_ms"] = round(self.latency_ms, 2)
        if self.error:
            result["error"] = self.error
        return result


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


async def check_database(engine: AsyncEngine | None = None) -> DependencyCheck:
    """Verify Postgres connectivity with a one-shot ``SELECT 1``.

    Opens its own short-lived connection from the shared engine so the probe
    never depends on the request-scoped ``get_db`` dependency. Returns an
    ``unavailable`` check (never raises) when the engine is missing or the
    connection/query fails.
    """
    start = time.perf_counter()
    resolved_engine = engine if engine is not None else database_state.engine
    if resolved_engine is None:
        return DependencyCheck("database", ok=False, error="not initialised")
    try:
        async with resolved_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("database_check_failed", error_type=type(exc).__name__)
        return DependencyCheck(
            "database",
            ok=False,
            latency_ms=_elapsed_ms(start),
            error="connection failed",
        )
    return DependencyCheck("database", ok=True, latency_ms=_elapsed_ms(start))


async def check_redis(settings: Settings, client: Redis | None = None) -> DependencyCheck:
    """Verify Redis connectivity with ``PING``.

    Reuses the injected/application-wide client when provided and never closes
    a client it does not own. Only a fallback client created here is closed.
    """
    start = time.perf_counter()
    owns_client = False
    try:
        if client is None:
            client = create_redis_client(settings)
            owns_client = True
        ok = await ping_redis(client)
        if not ok:
            return DependencyCheck(
                "redis",
                ok=False,
                latency_ms=_elapsed_ms(start),
                error="ping failed",
            )
    except Exception as exc:
        logger.warning("redis_check_failed", error_type=type(exc).__name__)
        return DependencyCheck(
            "redis",
            ok=False,
            latency_ms=_elapsed_ms(start),
            error="connection failed",
        )
    finally:
        if owns_client and client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.warning("redis_client_close_failed")
    return DependencyCheck("redis", ok=True, latency_ms=_elapsed_ms(start))


async def check_litellm_readiness(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> DependencyCheck:
    """Verify LiteLLM readiness through its private base URL.

    Reuses the injected/application-wide client when provided. Ownership is
    claimed only after the fallback client is constructed, so a construction
    failure cannot trigger ``aclose`` on ``None``.
    """
    start = time.perf_counter()
    owns_client = False
    try:
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(_READINESS_TIMEOUT_SECONDS, connect=2.0)
            )
            owns_client = True
        response = await client.get(
            f"{settings.litellm_base_url}/health/readiness",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
        )
        latency_ms = _elapsed_ms(start)
        if response.status_code == 200:
            return DependencyCheck("litellm", ok=True, latency_ms=latency_ms)
        return DependencyCheck(
            "litellm", ok=False, latency_ms=latency_ms, error=f"status {response.status_code}"
        )
    except Exception as exc:
        logger.warning("litellm_check_failed", error_type=type(exc).__name__)
        return DependencyCheck(
            "litellm",
            ok=False,
            latency_ms=_elapsed_ms(start),
            error="connection failed",
        )
    finally:
        if owns_client and client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.warning("litellm_client_close_failed")


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness probe: the process is running."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Readiness probe: every private dependency must be reachable.

    Always returns the per-dependency envelope with either 200 or 503; a
    missing or failing dependency is reported as ``unavailable`` rather than
    bubbling up as a 500.
    """
    redis_client = getattr(request.app.state, "redis", None)
    litellm_client = getattr(request.app.state, "litellm_client", None)
    checks = [
        await check_database(),
        await check_redis(settings, redis_client),
        await check_litellm_readiness(settings, litellm_client),
    ]
    all_ok = all(check.ok for check in checks)
    body: dict[str, Any] = {
        "status": "ok" if all_ok else "unavailable",
        "checks": {check.name: check.to_dict() for check in checks},
    }
    if not all_ok:
        logger.warning("readiness_failed", failing=[c.name for c in checks if not c.ok])
    return JSONResponse(status_code=200 if all_ok else 503, content=body)
