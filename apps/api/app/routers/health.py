"""Health check endpoints for liveness and readiness probes.

``/healthz`` is dependency-free for orchestrator liveness checks. ``/readyz``
verifies Postgres, Redis, and LiteLLM connectivity and returns 503 when any
dependency is unavailable. Responses never include credentials.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.redis import create_redis_client, ping_redis
from app.db.session import get_db

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


async def check_database(session: AsyncSession) -> DependencyCheck:
    """Verify Postgres connectivity with ``SELECT 1``."""
    start = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("database_check_failed", error_type=type(exc).__name__)
        return DependencyCheck(
            "database",
            ok=False,
            latency_ms=(time.perf_counter() - start) * 1000,
            error="connection failed",
        )
    return DependencyCheck("database", ok=True, latency_ms=(time.perf_counter() - start) * 1000)


async def check_redis(settings: Settings) -> DependencyCheck:
    """Verify Redis connectivity with ``PING``."""
    start = time.perf_counter()
    client = None
    try:
        client = create_redis_client(settings)
        ok = await ping_redis(client)
        if not ok:
            return DependencyCheck(
                "redis",
                ok=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                error="ping failed",
            )
    except Exception as exc:
        logger.warning("redis_check_failed", error_type=type(exc).__name__)
        return DependencyCheck(
            "redis",
            ok=False,
            latency_ms=(time.perf_counter() - start) * 1000,
            error="connection failed",
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
    return DependencyCheck("redis", ok=True, latency_ms=(time.perf_counter() - start) * 1000)


async def check_litellm_readiness(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> DependencyCheck:
    """Verify LiteLLM readiness through its private base URL."""
    start = time.perf_counter()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(_READINESS_TIMEOUT_SECONDS, connect=2.0))
    try:
        response = await client.get(
            f"{settings.litellm_base_url}/health/readiness",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
        )
        latency_ms = (time.perf_counter() - start) * 1000
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
            latency_ms=(time.perf_counter() - start) * 1000,
            error="connection failed",
        )
    finally:
        if owns_client:
            await client.aclose()


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness probe: the process is running."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Readiness probe: every private dependency must be reachable."""
    checks = [
        await check_database(session),
        await check_redis(settings),
        await check_litellm_readiness(settings),
    ]
    all_ok = all(check.ok for check in checks)
    body: dict[str, Any] = {
        "status": "ok" if all_ok else "unavailable",
        "checks": {check.name: check.to_dict() for check in checks},
    }
    if not all_ok:
        logger.warning("readiness_failed", failing=[c.name for c in checks if not c.ok])
    return JSONResponse(status_code=200 if all_ok else 503, content=body)
