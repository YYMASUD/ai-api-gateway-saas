# API Reference

## Scope and status

This document describes the FastAPI gateway's HTTP surface. Phase 1 implements health/readiness probes and the OpenAI-compatible error envelope. User-facing endpoints (inference, key management, billing, dashboard) are scaffolded only; their implementations arrive in later phases.

## Base URLs

| Environment | Public API | Dashboard (CORS-enabled) | Internal API |
| --- | --- | --- | --- |
| Local development | `http://localhost:8000` | `http://localhost:3000` | `http://api:8000` (Docker network) |
| Production | `https://<edge>/v1` | `https://<dashboard-origin>` | private, never exposed |

The API is published on the edge. The dashboard origin is added to CORS allowlist via `APP_URL`. The public `/v1` surface is **not** given browser CORS; it is intended for API-key clients, SDKs, and server-side callers.

## Common behaviors

### Request IDs

Every response includes an `X-Request-ID` header. If the request carried a sanitized `X-Request-ID`, it is echoed back; otherwise a UUIDv4 is generated. Use this value when reporting issues or correlating logs.

### Authentication

| Route group | Phase 1 | Phase 2+ |
| --- | --- | --- |
| `/healthz`, `/readyz` | no auth | no auth |
| `/v1/*` | no auth dependency wired | `Authorization: Bearer <api_key>` (SHA-256+pepper verified against `api_keys.key_hash`) |
| `/dashboard/*` | no auth dependency wired | `Authorization: Bearer <jwt>` (HS256, `exp`/`iss`/`aud` required) |
| `/billing/*`, `/webhooks/*`, `/admin/*` | not implemented | API key or JWT as appropriate; webhook signature verification |

Phase 1 ships the cryptographic helpers (`apps/api/app/core/security.py`) but does not yet wire them as FastAPI dependencies.

### Error envelope

All errors follow the OpenAI-compatible shape, regardless of the route:

```json
{
  "error": {
    "message": "Human-readable description",
    "type": "invalid_request_error",
    "param": null,
    "code": "not_found"
  }
}
```

| HTTP status | `type` | `code` |
| --- | --- | --- |
| 400 | `invalid_request_error` | (varies) |
| 401 | `authentication_error` | `invalid_api_key` |
| 403 | `permission_error` | `forbidden` |
| 404 | `invalid_request_error` | `not_found` |
| 405 | `invalid_request_error` | `method_not_allowed` |
| 422 | `invalid_request_error` | `invalid_request` (with `param` location) |
| 429 | `rate_limit_error` | `rate_limit_exceeded` |
| 502 | `api_error` | `upstream_error` |
| 5xx | `api_error` | `internal_error` |

Unhandled exceptions are logged with the request ID and returned as a generic `500` body; prompts, completions, and credentials are redacted from logs.

## Implemented endpoints (Phase 1)

### `GET /healthz`

Liveness probe. Returns `200` while the API process is running. No dependencies are checked.

```json
{ "status": "ok" }
```

### `GET /readyz`

Readiness probe. Validates that PostgreSQL, Redis, and LiteLLM are reachable. Returns `200` only when all checks succeed; otherwise `503` with per-dependency status.

```json
{
  "status": "ok",
  "checks": {
    "database": { "status": "ok", "latency_ms": 2.14 },
    "redis":    { "status": "ok", "latency_ms": 0.87 },
    "litellm":  { "status": "ok", "latency_ms": 12.03 }
  }
}
```

When a dependency is unavailable:

```json
{
  "status": "unavailable",
  "checks": {
    "database": { "status": "ok", "latency_ms": 1.9 },
    "redis":    { "status": "unavailable", "latency_ms": 2000.41, "error": "connection failed" },
    "litellm":  { "status": "ok", "latency_ms": 9.88 }
  }
}
```

The Next.js typed API client targets `/healthz` for its liveness probe, matching this endpoint. The future dashboard health panel should use the same path.

### `GET /docs` and `GET /openapi.json`

Interactive Swagger UI and the raw OpenAPI schema are available in non-production only (`environment != "production"`). They are disabled in production to avoid exposing internal routes.

## Planned endpoints (later phases)

These routes are registered but currently return placeholder responses. They are listed here so clients can be shaped against a stable contract.

### OpenAI-compatible inference

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `POST` | `/v1/chat/completions` | 2 | Streaming and non-streaming; API-key auth; forwards to LiteLLM with master key; records `usage_events` and settles credits |
| `GET` | `/v1/models` | 2 | Lists models the authenticated key is allowed to call; admin sees all |

### Dashboard

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `GET` | `/dashboard/*` | 3 | JWT-protected pages and data endpoints |
| `POST` | `/dashboard/auth/login` | 3 | Returns a signed JWT |
| `GET` | `/dashboard/keys` | 3 | Lists masked API keys |
| `POST` | `/dashboard/keys` | 3 | Issues a new API key; raw value returned once |
| `DELETE` | `/dashboard/keys/{id}` | 3 | Revokes a key |
| `GET` | `/dashboard/usage` | 3 | Per-key, per-day, per-model aggregates |
| `GET` | `/dashboard/billing` | 4 | Credit balance, plan, upcoming invoice |

### Billing and webhooks

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `POST` | `/billing/checkout` | 4 | Creates a Stripe Checkout session |
| `POST` | `/webhooks/stripe` | 4 | Verifies signature, idempotent event processing |

### Admin

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `*` | `/admin/*` | later | Internal tooling; restricted roles |

## Headers

| Header | Direction | Description |
| --- | --- | --- |
| `X-Request-ID` | both | Request correlation ID (sanitized; max 128 chars). |
| `Authorization` | request | `Bearer <api_key>` for `/v1`, `Bearer <jwt>` for `/dashboard`. |
| `Content-Type` | both | `application/json` for JSON bodies. |
| `Accept` | request | `application/json` expected. |

## CORS

CORS is applied to dashboard routes only. The allowed origin is derived from `APP_URL` (scheme + host + port). The public `/v1` API is not CORS-enabled; browser clients should not call it directly.

## Compatibility notes

- The `/v1` surface is intended to be OpenAI-compatible for request/response shapes and error codes. Streaming (`stream: true`) and advanced features (function calling, responses API) are Phase 2+.
- Money is represented as integer **microcredits** (`1,000,000 microcredits = 1 credit`). No floating-point currency values are stored or returned.
- Rate limiting, quota enforcement, and per-key spend caps are not enforced in Phase 1.

## Versioning and deprecation

No version prefix is used yet. When breaking changes are required, the API will introduce `/v2/` and maintain `/v1/` for a documented deprecation window. Deprecation headers and notices will be added before any breaking change.
