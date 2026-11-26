"""FastAPI application entrypoint for the AI API Gateway public API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import DashboardCORSMiddleware, RequestIDMiddleware
from app.db.redis import create_redis_client
from app.db.session import database_state
from app.routers import (
    admin,
    auth,
    billing,
    credits,
    dashboard,
    health,
    v1_chat,
    v1_models,
    webhooks,
)

logger = get_logger(__name__)


_READINESS_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise and tear down shared resources."""
    settings: Settings = app.state.settings
    await database_state.init(settings)
    redis_client: Redis = create_redis_client(settings)
    app.state.redis = redis_client
    # One shared client for readiness probes; closed exactly once on shutdown.
    litellm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(_READINESS_TIMEOUT_SECONDS, connect=2.0)
    )
    app.state.litellm_client = litellm_client
    logger.info("gateway_started", environment=settings.environment)
    try:
        yield
    finally:
        # Close each resource independently so one failure cannot skip the rest.
        for resource in (litellm_client, redis_client):
            try:
                await resource.aclose()
            except Exception:
                logger.warning("lifespan_cleanup_failed", resource=type(resource).__name__)
        await database_state.dispose()
        logger.info("gateway_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory used by uvicorn and tests."""
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)

    app = FastAPI(
        title="AI API Gateway",
        version="0.1.0",
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = resolved

    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(DashboardCORSMiddleware, allow_origins=resolved.cors_origins)

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/auth")
    app.include_router(v1_chat.router, prefix="/v1")
    app.include_router(v1_models.router, prefix="/v1")
    app.include_router(dashboard.router, prefix="/dashboard")
    app.include_router(billing.router, prefix="/billing")
    app.include_router(webhooks.router, prefix="/webhooks")
    app.include_router(admin.router, prefix="/admin")
    app.include_router(credits.router, prefix="/dashboard/credits")

    return app


__all__ = ["create_app", "lifespan"]
