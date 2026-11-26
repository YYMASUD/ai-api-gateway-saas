# API Reference

## Scope and status

This document describes the FastAPI gateway's HTTP surface.

- **Phase 1** implements health/readiness probes and the OpenAI-compatible error envelope.
- **Phase 2** implements self-built email/password authentication (`/auth/*`) and API-key lifecycle management (`/dashboard/keys*`); `/auth/me` and `/dashboard/keys*` are protected by HS256 JWTs, while `/auth/register` and `/auth/login` are public.
- **Phase 3** implements owner-only credit reads (`/dashboard/credits*`) and the internal credit ledger/reservation service (reserve, settle, refund, release_expired, grant_purchase); dashboard reads are protected by HS256 JWTs.
- **Phase 4** implements the OpenAI-compatible `/v1/*` surface (GET `/v1/models`, POST `/v1/chat/completions` with streaming and non-streaming), API-key scope enforcement, request size caps, Redis-backed rate limiting, and the reserve/settle/refund lifecycle wired through LiteLLM.
- **Phase 5** implements Stripe Checkout top-ups (`POST /billing/checkout`) and the Stripe webhook receiver (`POST /webhooks/stripe`); the **worker** grants credits idempotently after the API verifies the signature, records the event, and enqueues `process_webhook`. FastAPI never grants credits inline.
- **Later phases** add usage dashboards, admin tooling, and receipt email (deferred from Phase 5). Anything not in the "Implemented endpoints" sections below is *planned* and not registered yet.

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
| `/dashboard/credits`, `/dashboard/credits/ledger` | `Authorization: Bearer <jwt>` (HS256, `exp`/`iss`/`aud` required) | 3 (implemented) |
| `/v1/models` | `Authorization: Bearer <api_key>` (SHA-256+pepper verified against `api_keys.key_hash`); scope `models:read` | 4 (implemented) |
| `/v1/chat/completions` | `Authorization: Bearer <api_key>` (SHA-256+pepper verified against `api_keys.key_hash`); scope `chat:completions` | 4 (implemented) |
| `/billing/checkout` | `Authorization: Bearer <jwt>` (HS256) | 5 (implemented) |
| `/webhooks/stripe` | none (public); gated by Stripe signature over the raw body | 5 (implemented) |
| `/dashboard/usage`, `/dashboard/billing`, `/admin/*` | API key or JWT as appropriate | later (planned) |

Phase 2 implements email/password authentication and JWT issuance. The cryptographic helpers (`apps/api/app/core/security.py`) are wired as FastAPI dependencies for `/dashboard/*` routes. Phase 4 wires the same API-key auth dependency into `/v1/*` and adds per-scope enforcement.

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
| 402 | `invalid_request_error` | `insufficient_credits` / `monthly_spend_cap_exceeded` (credit-service reserve checks) |
| 403 | `permission_error` | `forbidden` / `user_suspended` / `insufficient_scope` (API key lacks the required scope for the endpoint) |
| 404 | `invalid_request_error` | `not_found` / `model_not_found` (requested model is not enabled or not priced) |
| 405 | `invalid_request_error` | `method_not_allowed` |
| 409 | `invalid_request_error` | `email_exists` (email already registered) / `idempotency_conflict` (reused idempotency key with different parameters) |
| 410 | `invalid_request_error` | `reservation_expired` (reservation passed `expires_at`) |
| 413 | `invalid_request_error` | `request_too_large` (request body exceeds `MAX_REQUEST_BYTES`) |
| 422 | `invalid_request_error` | `invalid_request` (with `param` location) / `amount_too_large` / `invalid_ttl` (credit-service bounds) / `max_tokens_exceeded` (requested `max_tokens` exceeds `MAX_OUTPUT_TOKENS`) |
| 429 | `rate_limit_error` | `rate_limit_exceeded` (per-key sliding-window or concurrent-cap breach; response includes `Retry-After` header in seconds) |
| 400 | `invalid_request_error` | `invalid_signature` (Stripe webhook signature missing/invalid, or webhook secret unconfigured; in addition to the general 400 cases) |
| 502 | `api_error` | `upstream_error` (LiteLLM returned a non-timeout failure, no routable model, or Stripe Checkout creation failed) |
| 503 | `api_error` | `stripe_not_configured` (`STRIPE_SECRET_KEY` blank; billing disabled locally) |
| 504 | `api_error` | `upstream_timeout` (LiteLLM or the upstream provider did not respond within the read timeout) |
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

**Redis lookup cache:** API-key lookups are cached in Redis for 60 seconds. Revocation deletes the entry immediately (see below). The cache backs the API-key auth dependency consumed by the `/v1/*` inference routes (implemented in Phase 4).

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

## Implemented endpoints (Phase 3)

Phase 3 adds the credit ledger service and owner-only dashboard credit reads. All amounts are integer **microcredits** (`1,000,000 microcredits = 1 credit`); no floating-point values are stored or returned.

Accounting model:

- `balance` = sum of the user's `credit_ledger` movements (append-only source of truth).
- `reserved` = sum of `amount_microcredits` over the user's **open** `credit_reservations`.
- `available` = `balance - reserved`; new reservations draw against `available`.

### Dashboard credits

All `/dashboard/credits*` endpoints require `Authorization: Bearer <jwt>` and are **owner-only**: they return data for the JWT's `sub` only. There is no parameter to query another user's credits (admin tooling is a later phase).

#### `GET /dashboard/credits`

Return the authenticated user's credit balance summary.

**Headers:** `Authorization: Bearer <jwt>`

**Response (200):**

```json
{
  "balance_microcredits": 5000000,
  "reserved_microcredits": 250000,
  "available_microcredits": 4750000,
  "lifetime_purchased_microcredits": 5000000,
  "lifetime_spent_microcredits": 0
}
```

**Status codes:**

| Status | Meaning |
| --- | --- |
| 200 | Balance summary returned |
| 401 | Missing or invalid JWT |
| 403 | The authenticated account is suspended (`user_suspended`) |

#### `GET /dashboard/credits/ledger`

List the authenticated user's ledger entries, newest first. The ledger is **append-only**: entries are never updated or deleted (enforced by a database trigger), so a given entry ID always returns the same content. Corrections appear as new compensating entries.

**Headers:** `Authorization: Bearer <jwt>`

**Query parameters (pagination):**

| Parameter | Type | Default | Constraints |
| --- | --- | --- | --- |
| `limit` | integer | 50 | 1–100 |
| `cursor` | string (ledger entry UUID) | none | Keyset cursor: pass the previous page's `next_cursor`. Unknown or foreign cursors are ignored and the first page is returned. |

**Response (200):**

```json
{
  "items": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440002",
      "type": "settlement",
      "amount_microcredits": -1200,
      "reference_id": "req_01J...",
      "metadata": { "reservation_id": "550e8400-e29b-41d4-a716-446655440003" },
      "created_at": "2026-11-26T14:22:10Z"
    }
  ],
  "next_cursor": "550e8400-e29b-41d4-a716-446655440002"
}
```

`type` is one of `purchase`, `reservation`, `settlement`, `refund`, `adjustment`. `amount_microcredits` is signed: positive credits the account, negative debits it; `reservation` and `refund` rows carry `0` because the hold itself lives in `credit_reservations`. Entries are ordered newest first (`created_at` descending, then `id` descending). Pass `next_cursor` as `cursor` to fetch the next page; `next_cursor` is `null` on the last page.

**Status codes:**

| Status | Meaning |
| --- | --- |
| 200 | Page of ledger entries returned (may be empty) |
| 401 | Missing or invalid JWT |
| 403 | The authenticated account is suspended (`user_suspended`) |
| 422 | Validation error (`limit` out of range) |

### Credit service semantics (internal)

The credit service provides `reserve`, `settle`, `refund`, `release_expired`, and `grant_purchase` operations. They are service-layer operations introduced in Phase 3 and wired into the public `/v1/chat/completions` route in Phase 4. They share these guarantees:

- **Idempotency:** ledger operations use the caller-supplied idempotency key plus a fingerprint (user, amount, reference); reservations use `(user_id, request_id)` uniqueness. Retries with the same key and parameters return the original outcome; reusing a key with different parameters is a `409 idempotency_conflict`. Reservation retries keep the original expiry (TTL is not fingerprinted).
- **Row locking:** transactions lock rows with `SELECT ... FOR UPDATE` in a fixed order — `users` → `credit_balances` → `credit_reservations` — to avoid deadlocks.
- **Reservation lifecycle:** `reserve` inserts an `open` reservation (unique per `(user_id, request_id)`, default expiry **15 minutes**) plus a zero-amount `reservation` ledger row; only `reserved` increases. `settle` closes it as `settled`, releases the whole hold, and debits the actual cost (`cost_microcredits ≤ amount_microcredits`) via a negative `settlement` ledger row; the unused difference becomes available again. `refund` and `release_expired` close it as `released`, freeing the hold with a zero-amount `refund` ledger row — `balance` is unchanged. `grant_purchase` credits a positive `purchase` entry to the ledger.
- **Reserve checks:** insufficient available credits → `402 insufficient_credits`; exceeding `users.spend_cap_monthly_microcredits` for the current UTC calendar month → `402 monthly_spend_cap_exceeded` (0 disables the cap). The month window is computed in UTC in application code, so it does not depend on the database session timezone.
- **Integer bounds:** every resulting `balance`, `reserved`, and lifetime total is validated against the signed `BIGINT` range *before* any mutation (`422 amount_too_large`); reservation TTL is capped at 86400 seconds (`422 invalid_ttl`).
- **Expiry:** settling a reservation past `expires_at` returns `410 reservation_expired`; unknown or foreign reservations return `404 reservation_not_found` (existence is not revealed).

## Implemented endpoints (Phase 4)

Phase 4 adds the OpenAI-compatible inference surface. All `/v1/*` endpoints authenticate with an **API key** (`Authorization: Bearer sk_live_...`), not a dashboard JWT, and enforce a per-endpoint **scope** on that key. The public `/v1` surface is **not** browser-CORS-enabled.

### Auth and scopes on `/v1`

| Endpoint | Required scope |
| --- | --- |
| `GET /v1/models` | `models:read` |
| `POST /v1/chat/completions` | `chat:completions` |

- A missing, malformed, or revoked key returns `401 invalid_api_key`.
- A valid key lacking the endpoint's scope returns `403 insufficient_scope`.
- A key whose owner is suspended returns `403 user_suspended`.
- A key with only `models:read` stays **read-only**: it cannot call chat completions.

### `GET /v1/models`

List the **enabled** model pricing rows as an OpenAI-compatible model list. This is the catalog the authenticated key is allowed to target.

**Headers:** `Authorization: Bearer <api_key>` with scope `models:read`.

**Response (200):**

```json
{
  "object": "list",
  "data": [
    {
      "id": "gateway-default",
      "object": "model",
      "created": 1764140400,
      "owned_by": "gateway"
    }
  ]
}
```

Only rows that are enabled in `model_pricing` are returned. `GET /v1/models` is a metadata read: it does **not** reserve credits, touch the ledger, or call LiteLLM.

**Status codes:**

| Status | Meaning |
| --- | --- |
| 200 | Model list returned (may be empty if no models are enabled) |
| 401 | Missing/invalid/revoked API key |
| 403 | Key lacks `models:read` (`insufficient_scope`) or owner is suspended (`user_suspended`) |

### `POST /v1/chat/completions`

OpenAI-compatible chat completions, forwarded to LiteLLM. Supports **streaming** (`stream: true`) and **non-streaming** requests.

**Headers:** `Authorization: Bearer <api_key>` with scope `chat:completions`; `Content-Type: application/json`.

**Request:**

```json
{
  "model": "gateway-default",
  "messages": [
    { "role": "user", "content": "Hello!" }
  ],
  "stream": false,
  "max_tokens": 256
}
```

`model` must reference an enabled model. `stream` and `max_tokens` are optional; `max_tokens` is bounded by `MAX_OUTPUT_TOKENS` (default 4096).

**Response (200, non-streaming):** an OpenAI-compatible completion object.

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1764140400,
  "model": "gateway-default",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "Hi there!" },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20
  }
}
```

**Streaming (`stream: true`):** the response is `text/event-stream` (Server-Sent Events) proxied **unbuffered** from LiteLLM. Each event is a `data: {chat.completion.chunk}` line; the stream terminates with `data: [DONE]`. If the client disconnects mid-stream, only the tokens actually produced up to that point are charged, and the `usage_events` row is recorded with status `partial`.

**Status codes:**

| Status | Meaning |
| --- | --- |
| 200 | Completion returned (or stream opened) |
| 400 | Malformed request body |
| 401 | Missing/invalid/revoked API key |
| 402 | `insufficient_credits` / `monthly_spend_cap_exceeded` on reserve |
| 403 | Key lacks `chat:completions` (`insufficient_scope`) or owner is suspended (`user_suspended`) |
| 404 | `model_not_found` — model is not enabled or not priced |
| 413 | `request_too_large` — body exceeds `MAX_REQUEST_BYTES` |
| 422 | `max_tokens_exceeded` — requested `max_tokens` exceeds `MAX_OUTPUT_TOKENS`, or shape validation fails |
| 429 | `rate_limit_exceeded` — per-key rate or concurrency limit hit; see `Retry-After` |
| 502 | `upstream_error` — LiteLLM returned an error, or no routable model is configured |
| 504 | `upstream_timeout` — upstream did not respond within the read timeout |

### Request flow and credit lifecycle

`POST /v1/chat/completions` executes this order (a failure at any stage stops the request and returns the matching error; the reservation is never charged for work not forwarded):

1. **Size cap** — reject bodies larger than `MAX_REQUEST_BYTES` with `413 request_too_large`.
2. **Validation** — parse and validate the OpenAI request shape (`max_tokens ≤ MAX_OUTPUT_TOKENS`, known model, `stream` boolean, etc.).
3. **Rate limit** — per-API-key sliding window (`RATE_LIMIT_REQUESTS_PER_MINUTE`) plus concurrent in-flight cap (`RATE_LIMIT_MAX_CONCURRENT`); breach returns `429 rate_limit_exceeded` with a `Retry-After` header.
4. **Reserve** — open a credit reservation for the estimated cost. A **floor of 1 microcredit** is reserved even when the estimate is 0, so zero-priced models still exercise the hold. Insufficient available credits or a monthly-cap breach returns `402`.
5. **Forward** — call LiteLLM over the private network with the master key, streaming if requested.
6. **Settle or refund** — on success, settle the reservation to the true cost (charging the actual usage and releasing the unused hold). On upstream failure (`502`/`504`) or client abort before any billable tokens, the reservation is **fully refunded** so no credits are consumed for failed work.
7. **Usage event** — record a `usage_events` row (idempotent on `request_id`) with tokens, cost, latency, and status (`success` / `partial` / `error`).

Reservations are held for `RESERVATION_TTL_SECONDS` (default 900s) and are swept by the worker's `release_expired`, so a crashed request cannot strand credits indefinitely.

### Rate limiting semantics

- **Sliding-window request limit:** each API key gets `RATE_LIMIT_REQUESTS_PER_MINUTE` requests per rolling minute (default 60), tracked in Redis.
- **Concurrent in-flight cap:** each key may have at most `RATE_LIMIT_MAX_CONCURRENT` requests in flight (default 8).
- **`Retry-After`:** a `429 rate_limit_exceeded` response includes a `Retry-After` header (integer seconds) indicating when the client may retry.
- **Fail-open on Redis outage:** if Redis is unavailable, rate limiting **fails open** — requests are allowed rather than blocked, so a cache outage does not take down inference. This is a deliberate availability-over-strictness tradeoff; credit reservation and spend caps (backed by PostgreSQL) still apply.

### Local development behavior (expected)

Local dev seeds **one placeholder pricing row** `gateway-default` at **0 microcredits** per 1k input/output tokens, and the LiteLLM `model_list` stays **empty** (no provider configured).

- `GET /v1/models` returns the single `gateway-default` entry.
- `POST /v1/chat/completions` passes auth, size, validation, rate-limit, and reservation (the 1-microcredit floor), then LiteLLM has no routable model and returns an error. The gateway maps this to **`502 upstream_error`** and **fully refunds** the reservation. Credits are not consumed.
- This 502 is **expected** in local dev and confirms the reserve/refund lifecycle works end-to-end before a real provider is configured.

## Implemented endpoints (Phase 5)

Phase 5 adds Stripe Checkout credit top-ups and the Stripe webhook receiver. **FastAPI never grants credits inline.** It verifies the webhook signature against the raw body, records the event idempotently, and enqueues `process_webhook`; the worker grants the credits idempotently, marks the payment `completed`, and sets `webhook_events.processed_at`. Receipt email is **deferred to Phase 6**.

### Credit packs and integer math

The approved conversion rule is **$1 = 1 credit** and **1 cent = 10,000 microcredits** (`1 credit = 1,000,000 microcredits`). All amounts are integers; no floats or rounding are used.

Approved Phase 5 pack catalog (values as specified by the Phase 5 decision):

| `pack_id` | `amount_cents` | `credits` | `microcredits` |
| --- | --- | --- | --- |
| `pack_10` | 1000 | 10 | 10,000,000 |
| `pack_50` | 5000 | 50 | 50,000,000 |
| `pack_100` | 10000 | 100 | 100,000,000 |

All values are derived with pure integer arithmetic: `microcredits = amount_cents × 10,000`.

### `POST /billing/checkout`

Create a Stripe Checkout session for a credit pack. JWT required; owner-only (the JWT `sub` identifies the buyer).

**Headers:** `Authorization: Bearer <jwt>`; `Content-Type: application/json`.

**Request:**

```json
{
  "pack_id": "pack_10",
  "success_url": "https://dashboard.example.com/dashboard/billing?checkout=success",
  "cancel_url": "https://dashboard.example.com/dashboard/billing?checkout=cancelled"
}
```

`pack_id` is required and must be one of `pack_10`, `pack_50`, `pack_100`. `success_url` and `cancel_url` are optional; omitted values fall back to `STRIPE_SUCCESS_URL` / `STRIPE_CANCEL_URL` (local defaults point at `http://localhost:3000/dashboard/billing`). Redirect URLs must be HTTPS, or plain HTTP on a local host (`localhost`, `127.0.0.1`, `::1`).

**Response (201):**

```json
{
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_...",
  "session_id": "cs_test_...",
  "amount_cents": 1000,
  "credits": 10,
  "microcredits": 100000
}
```

The response contains the hosted Checkout URL and the integer amounts for the selected pack. It does **not** grant credits; the grant happens only after a verified webhook is processed by the worker.

**Status codes and errors:**

| Status | `code` / shape | Meaning |
| --- | --- | --- |
| 201 | success envelope above | Checkout session created; redirect the browser to `checkout_url` |
| 401 | `authentication_error` / `invalid_token` | No JWT, or missing/invalid/expired JWT |
| 422 | `invalid_request_error` / `invalid_request` | Unknown `pack_id` (schema validation), malformed body, or a redirect URL that is neither HTTPS nor a local HTTP URL |
| 503 | `api_error` / `stripe_not_configured` | Stripe is not configured: `STRIPE_SECRET_KEY` is blank; billing is disabled locally |
| 502 | `api_error` / `upstream_error` | Stripe API failure while creating the session (implemented behavior beyond the base contract) |

### `POST /webhooks/stripe`

Public webhook receiver — **no authentication**. Requests are gated by Stripe signature verification against the **raw request body** using `STRIPE_WEBHOOK_SECRET` and the `Stripe-Signature` header. This endpoint grants nothing itself.

**Headers:** `Stripe-Signature: <stripe signature header>`; `Content-Type: application/json`. No `Authorization` header is sent or required.

**Request:** the exact JSON body delivered by Stripe. Do not re-serialize or pretty-print it before sending; verification uses the byte-for-byte payload.

```json
{
  "id": "evt_...",
  "object": "event",
  "type": "checkout.session.completed",
  "data": { "object": { "id": "cs_test_..." } }
}
```

**Response (200, first delivery):**

```json
{ "status": "queued" }
```

**Response (200, replay of an already-recorded event):**

```json
{ "status": "already_processed" }
```

**Response (400, bad signature):**

```json
{
  "error": {
    "message": "Invalid webhook signature",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_signature"
  }
}
```

**Status codes and errors:**

| Status | Meaning |
| --- | --- |
| 200 `{"status":"queued"}` | Signature valid, event recorded, `process_webhook` enqueued. An **unprocessed** replay also returns `queued` and re-enqueues for recovery. |
| 200 `{"status":"already_processed"}` | Signature valid, the event is already recorded **and processed**; no duplicate job enqueued |
| 400 `invalid_signature` | Signature missing/invalid, or the webhook secret is not configured; no event recorded, no job enqueued |
| 200 `{"status":"ignored"}` | Valid event of a type other than `checkout.session.completed` / `checkout.session.async_payment_succeeded`: recorded but not enqueued |

Payloads and customer data are never written to logs — only the opaque event ID and event type.

### Worker credit grant and replay guarantee

`process_webhook(event_id)` is executed by the private ARQ worker, which shares the API's credit package. It handles `checkout.session.completed` and `checkout.session.async_payment_succeeded` only when `payment_status == "paid"`, the currency is USD, and `amount_total` matches a catalog pack. For a valid paid event it:

1. Resolves the stored event and payment, verifies the payment belongs to the same user and has the same amount/session, and uses `idempotency_key = stripe:<session_id>` for the ledger grant.
2. Calls the credit service's `grant_purchase` to add the pack's microcredits to the append-only `credit_ledger` (and materialised `credit_balances`) exactly once.
3. Marks the `payments` row `completed` and sets `webhook_events.processed_at`.
4. Leaves unpaid / non-USD / non-catalog events unprocessed and retryable with a short `webhook_events.error`; a replayed unprocessed event can therefore be re-enqueued safely.

**Replay guarantee:** delivering the same paid webhook **five times** results in **one** credit grant. While the event is unprocessed, deliveries may each re-enqueue a recovery job (safe); once `processed_at` is set, deliveries return `already_processed` without a new job. The ledger idempotency key `stripe:<session_id>` makes any duplicate worker run a no-op. Never delete `webhook_events` rows or ledger idempotency keys to "force" a retry — see [docs/RUNBOOK.md](RUNBOOK.md).

## Planned endpoints (later phases)

These remaining routes are not registered yet and currently return `404 not_found`; their routers exist as empty placeholders. They are listed here so clients can be shaped against a stable contract. The `/v1/*` routes documented above are implemented in Phase 4 and are not listed here.

### Dashboard (additional endpoints)

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `GET` | `/dashboard/usage` | later | Per-key, per-day, per-model aggregates |
| `GET` | `/dashboard/billing` | later | Plan and upcoming invoice; the credit balance itself is already served by `GET /dashboard/credits` (Phase 3) |

> **Note:** Authentication (`POST /auth/register`, `POST /auth/login`, `GET /auth/me`) and API-key lifecycle (`GET/POST/DELETE /dashboard/keys*`) are implemented in Phase 2. The credit ledger and owner-only dashboard credit reads (`GET /dashboard/credits`, `GET /dashboard/credits/ledger`) are implemented in Phase 3. The OpenAI-compatible `/v1/models` and `/v1/chat/completions` routes are implemented in Phase 4. Stripe Checkout and the Stripe webhook receiver are implemented in Phase 5. See the "Implemented endpoints" sections above.

### Billing and webhooks

`POST /billing/checkout` and `POST /webhooks/stripe` are **implemented in Phase 5** (see [Implemented endpoints (Phase 5)](#implemented-endpoints-phase-5)). Receipt email and any provider beyond Stripe remain later phases.

### Admin

| Method | Path | Phase | Notes |
| --- | --- | --- | --- |
| `*` | `/admin/*` | later | Internal tooling; restricted roles |

## Headers

| Header | Direction | Description |
| --- | --- | --- |
| `X-Request-ID` | both | Request correlation ID (sanitized; max 128 chars). |
| `Authorization` | request | `Bearer <jwt>` for `/auth/me`, `/dashboard/*`, and `/billing/checkout`; `Bearer <api_key>` for `/v1` (Phase 4). `/webhooks/stripe` uses no `Authorization` (signature-gated). |
| `Content-Type` | both | `application/json` for JSON bodies. |
| `Accept` | request | `application/json` expected. |

## CORS

CORS is applied to all routes except the public `/v1` surface. The allowed origin is derived from `APP_URL` (scheme + host + port), which is why the dashboard origin can call `/auth/*` and `/dashboard/*` directly with a Bearer token. The public `/v1` API is not CORS-enabled; browser clients should not call it directly.

Because the browser currently sends the JWT as a Bearer header (see the interim `sessionStorage` note), CORS allows the `Authorization` header. Once the dashboard moves to HttpOnly cookies behind a BFF, the browser will stop holding the token and `allow_credentials` will be scoped to the BFF origin.

## Compatibility notes

- The `/v1` surface is intended to be OpenAI-compatible for request/response shapes and error codes. Streaming (`stream: true`) is supported as of Phase 4; advanced features (function calling, responses API) are later phases.
- Money is represented as integer **microcredits** (`1,000,000 microcredits = 1 credit`). No floating-point currency values are stored or returned.
- As of Phase 4, `POST /v1/chat/completions` enforces the request size cap, validation, per-key rate limiting, and the credit reserve/settle/refund lifecycle; `GET /v1/models` lists enabled models. The Phase 3 credit service remains authoritative for balance, reservations, and the monthly cap.
- Auth and API-key management are implemented in Phase 2; the credit ledger and dashboard credit reads are implemented in Phase 3; the OpenAI-compatible `/v1` inference surface is implemented in Phase 4; Stripe Checkout and signature-verified, worker-granted webhooks are implemented in Phase 5. Usage dashboards, admin tooling, and receipt email remain later phases.

## Versioning and deprecation

No version prefix is used yet. When breaking changes are required, the API will introduce `/v2/` and maintain `/v1/` for a documented deprecation window. Deprecation headers and notices will be added before any breaking change.
