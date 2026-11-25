# API Reference

## Scope and status

This document describes the FastAPI gateway's HTTP surface.

- **Phase 1** implements health/readiness probes and the OpenAI-compatible error envelope.
- **Phase 2** implements self-built email/password authentication (`/auth/*`) and API-key lifecycle management (`/dashboard/keys*`); `/auth/me` and `/dashboard/keys*` are protected by HS256 JWTs, while `/auth/register` and `/auth/login` are public.
- **Later phases** add inference (`/v1/*`), billing/webhooks (`/billing/*`, `/webhooks/*`), usage data, and admin tooling. These are documented as *planned* and are not implemented yet.

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

| Route group | Auth mechanism | Phase |
| --- | --- | --- |
| `/healthz`, `/readyz` | none | 1 (implemented) |
| `/auth/register`, `/auth/login` | none (public) | 2 (implemented) |
| `/auth/me` | `Authorization: Bearer <jwt>` (HS256, `exp`/`iss`/`aud` required) | 2 (implemented) |
| `/dashboard/keys*` | `Authorization: Bearer <jwt>` (HS256, `exp`/`iss`/`aud` required) | 2 (implemented) |
| `/v1/*` | `Authorization: Bearer <api_key>` (SHA-256+pepper verified against `api_keys.key_hash`) | later (planned) |
| `/billing/*`, `/webhooks/*`, `/admin/*` | API key or JWT as appropriate; webhook signature verification | later (planned) |

Phase 2 implements email/password authentication and JWT issuance. The cryptographic helpers (`apps/api/app/core/security.py`) are wired as FastAPI dependencies for `/dashboard/*` routes. Inference (`/v1/*`) auth dependencies arrive in a later phase.

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
| 401 | `authentication_error` | `invalid_api_key` / `invalid_credentials` / `invalid_token` |
| 403 | `permission_error` | `forbidden` / `user_suspended` (`insufficient_scope` arrives with /v1 scope enforcement) |
| 404 | `invalid_request_error` | `not_found` |
| 405 | `invalid_request_error` | `method_not_allowed` |
| 409 | `invalid_request_error` | `email_exists` (email already registered) |
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

## Implemented endpoints (Phase 2)

Phase 2 adds self-built email/password authentication and API-key lifecycle management.

### Authentication

#### `POST /auth/register`

Create a new user account.

**Request:**

```json
{
  "email": "user@example.com",
  "password": "securepassword123",
  "display_name": "Optional Name"
}
```

`display_name` is optional.

**Response (201):** Same envelope as `POST /auth/login` — the new account is created and a token is issued immediately.

```json
{
  "access_token": "<jwt-access-token>",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "email": "user@example.com",
    "display_name": null,
    "role": "user",
    "status": "active",
    "created_at": "2026-11-26T10:30:00Z"
  }
}
```

**Status codes:**

| Status | Meaning |
| --- | --- |
| 201 | Account created and token issued |
| 409 | Email already registered |
| 422 | Validation error (bad email, or password outside 12–128 chars) |

Password policy: minimum 12 characters, maximum 128 characters. Passwords are hashed with Argon2id before storage. Email addresses are normalised to lowercase and must be unique.

#### `POST /auth/login`

Authenticate and receive a JWT access token.

**Request:**

```json
{
  "email": "user@example.com",
  "password": "securepassword123"
}
```

**Response (200):**

```json
{
  "access_token": "<jwt-access-token>",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "email": "user@example.com",
    "display_name": null,
    "role": "user",
    "status": "active",
    "created_at": "2026-11-26T10:30:00Z"
  }
}
```

**Status codes:**

| Status | Meaning |
| --- | --- |
| 200 | Login successful; JWT issued |
| 401 | Invalid credentials (generic response; does not distinguish unknown email, wrong password, or suspended account) |
| 422 | Validation error (missing or malformed fields) |

Suspended accounts receive the same generic `401` on login; `403 user_suspended` is only returned on already-authenticated routes such as `GET /auth/me`.

The JWT contains `sub` (user ID), `exp`, `iss`, and `aud` claims. The `expires_in` value is in seconds and matches `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default 60 minutes, range 5–1440).

**Interim token handling:** The Phase 2 web UI stores the JWT in `sessionStorage` so the keys page can call FastAPI directly. Production deployments must migrate to HttpOnly cookies behind a BFF (see `docs/SECURITY.md`).

#### `GET /auth/me`

Retrieve the authenticated user's profile.

**Headers:** `Authorization: Bearer <jwt>`

**Response (200):**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "user@example.com",
  "display_name": null,
  "role": "user",
  "status": "active",
  "created_at": "2026-11-26T10:30:00Z"
}
```

**Status codes:**

| Status | Meaning |
| --- | --- |
| 200 | Authenticated user returned |
| 401 | Missing or invalid JWT |
| 403 | The authenticated account is suspended (`user_suspended`) |

### Dashboard API keys

All `/dashboard/keys*` endpoints require `Authorization: Bearer <jwt>`.

#### `GET /dashboard/keys`

List all API keys for the authenticated user. Raw key values are never returned; only the masked `sk_live_XXXXXXXX` prefix is shown.

**Headers:** `Authorization: Bearer <jwt>`

**Response (200):**

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440001",
    "name": "Production key",
    "prefix": "sk_live_XXXXXXXX",
    "scopes": ["chat:completions", "models:read"],
    "created_at": "2026-11-26T10:30:00Z",
    "last_used_at": "2026-11-26T14:22:10Z",
    "revoked_at": null
  }
]
```

**Status codes:**

| Status | Meaning |
| --- | --- |
| 200 | Keys listed (may be empty) |
| 401 | Missing or invalid JWT |

#### `POST /dashboard/keys`

Create a new API key. The raw key is returned **exactly once** in the response and is never stored or retrievable afterward (the value below is an illustrative placeholder).

**Headers:** `Authorization: Bearer <jwt>`

**Request:**

```json
{
  "name": "Production key",
  "scopes": ["chat:completions", "models:read"]
}
```

**Response (201):**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440001",
  "name": "Production key",
  "key": "sk_live_<redacted-once>",
  "prefix": "sk_live_XXXXXXXX",
  "scopes": ["chat:completions", "models:read"],
  "created_at": "2026-11-26T10:30:00Z",
  "last_used_at": null,
  "revoked_at": null
}
```

**Status codes:**

| Status | Meaning |
| --- | --- |
| 201 | Key created; raw value returned once |
| 401 | Missing or invalid JWT |
| 422 | Validation error (blank/oversized name, unknown scope) |

**Key format:** `sk_live_` prefix followed by base62-encoded random material generated from 32 cryptographically random bytes. The stored `key_hash` is `SHA-256(raw_key + API_KEY_PEPPER)`, hex-encoded; the raw key is never persisted. The `prefix` column stores the first 8 characters of the random part; the API renders it as `sk_live_XXXXXXXX` for display.

**Scopes:** Currently supported scopes are `chat:completions` and `models:read`. The field is optional and defaults to both scopes; an explicit list must contain at least one known scope (max 2, duplicates deduplicated).

**Redis lookup cache:** API-key lookups are cached in Redis for 60 seconds. Revocation deletes the entry immediately (see below). The cache backs the API-key auth dependency; the `/v1/*` inference routes that consume it arrive in a later phase.

**Audit log:** Key creation is recorded in `audit_logs` with actor, action (`api_key.create`), and resource ID. The raw key and hash are never logged.

`last_used_at` is updated when an API key authenticates and the lookup misses the Redis cache; with the 60-second positive cache, its effective granularity is approximately 60 seconds.

#### `DELETE /dashboard/keys/{id}`

Revoke an API key.

**Headers:** `Authorization: Bearer <jwt>`

**Response (204):** No content.

**Status codes:**

| Status | Meaning |
| --- | --- |
| 204 | Key revoked successfully (or already revoked) |
| 401 | Missing or invalid JWT |
| 404 | Key not found, or it belongs to another user (existence is not revealed) |

**Revocation behavior:** The `revoked_at` timestamp is set on the `api_keys` row and the Redis cache entry for the key hash is deleted immediately, so the key stops authenticating on the next request. If the cache delete fails, the key remains usable for at most the 60-second cache TTL (stale-but-bounded). Revocation is idempotent at the data level; revoking an already-revoked key is a no-op that still returns `204`.

**Audit log:** Key revocation is recorded in `audit_logs` with actor, action (`api_key.revoke`), and resource ID.

## Planned endpoints (later phases)

These routes are not registered yet and currently return `404 not_found`; their routers exist as empty placeholders. They are listed here so clients can be shaped against a stable contract.

### OpenAI-compatible inference

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `POST` | `/v1/chat/completions` | later | Streaming and non-streaming; API-key auth; forwards to LiteLLM with master key; records `usage_events` and settles credits |
| `GET` | `/v1/models` | later | Lists models the authenticated key is allowed to call; admin sees all |

### Dashboard (additional endpoints)

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `GET` | `/dashboard/usage` | later | Per-key, per-day, per-model aggregates |
| `GET` | `/dashboard/billing` | later | Credit balance, plan, upcoming invoice |

> **Note:** Authentication (`POST /auth/register`, `POST /auth/login`, `GET /auth/me`) and API-key lifecycle (`GET/POST/DELETE /dashboard/keys*`) are implemented in Phase 2. See the "Implemented endpoints (Phase 2)" section above.

### Billing and webhooks

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `POST` | `/billing/checkout` | later | Creates a Stripe Checkout session |
| `POST` | `/webhooks/stripe` | later | Verifies signature, idempotent event processing |

### Admin

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `*` | `/admin/*` | later | Internal tooling; restricted roles |

## Headers

| Header | Direction | Description |
| --- | --- | --- |
| `X-Request-ID` | both | Request correlation ID (sanitized; max 128 chars). |
| `Authorization` | request | `Bearer <jwt>` for `/auth/me` and `/dashboard/*`; `Bearer <api_key>` for `/v1` (later phase). |
| `Content-Type` | both | `application/json` for JSON bodies. |
| `Accept` | request | `application/json` expected. |

## CORS

CORS is applied to all routes except the public `/v1` surface. The allowed origin is derived from `APP_URL` (scheme + host + port), which is why the dashboard origin can call `/auth/*` and `/dashboard/*` directly with a Bearer token. The public `/v1` API is not CORS-enabled; browser clients should not call it directly.

Because the browser currently sends the JWT as a Bearer header (see the interim `sessionStorage` note), CORS allows the `Authorization` header. Once the dashboard moves to HttpOnly cookies behind a BFF, the browser will stop holding the token and `allow_credentials` will be scoped to the BFF origin.

## Compatibility notes

- The `/v1` surface is intended to be OpenAI-compatible for request/response shapes and error codes. Streaming (`stream: true`) and advanced features (function calling, responses API) are later phases.
- Money is represented as integer **microcredits** (`1,000,000 microcredits = 1 credit`). No floating-point currency values are stored or returned.
- Rate limiting, quota enforcement, and per-key spend caps are not enforced yet.
- Auth and API-key management are implemented in Phase 2; inference (`/v1/*`), billing, webhooks, usage data, and admin tooling remain later phases.

## Versioning and deprecation

No version prefix is used yet. When breaking changes are required, the API will introduce `/v2/` and maintain `/v1/` for a documented deprecation window. Deprecation headers and notices will be added before any breaking change.
