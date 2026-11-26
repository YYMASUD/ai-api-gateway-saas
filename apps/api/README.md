# API Gateway (FastAPI)

The only public service allowed to reach Postgres, Redis, and LiteLLM. All
configuration is loaded from environment variables; there are no secret
defaults. The service intentionally has no `.env` loading: supply values via
the environment or a secrets manager.

## Configuration

Required environment variables:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://...` async DSN |
| `REDIS_URL` | `redis://...` or `rediss://...` |
| `LITELLM_MASTER_KEY` | private LiteLLM proxy key |
| `JWT_SECRET` | dashboard JWT signing secret (HS256) |
| `API_KEY_PEPPER` | server-side pepper for API key SHA-256 hashing |

All of the above must be non-blank; startup fails fast otherwise. Never commit
real values.

Optional: `ENVIRONMENT`, `LOG_LEVEL`, `APP_URL`,
`LITELLM_BASE_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_ALGORITHM` (must be
`HS256`), `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default `60`, allowed range
`5`–`1440`), `HTTP_CONNECT_TIMEOUT`, `HTTP_READ_TIMEOUT`,
`SPEND_ALERT_THRESHOLD_MICROCREDITS`.

## Local development

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn app.main:create_app --factory --reload
```

## Migrations

```bash
alembic upgrade head
```

Offline SQL rendering does not need a database connection:

```bash
alembic upgrade head --sql
```

## Schema and money units

The initial migration creates all nine Phase 1 tables with UUID primary keys
and `created_at` / `updated_at` timestamps on every table.

All money columns are PostgreSQL `BIGINT` integers; no float or numeric types
are used:

| Fields | Unit |
| --- | --- |
| `users.spend_cap_monthly_cents`, `payments.amount_cents` | Fiat cents |
| `credit_ledger.amount_microcredits`, `credit_balances.balance`, `credit_balances.reserved`, `usage_events.cost_microcredits`, `payments.credits_granted` | Microcredits (`1,000,000 microcredits = 1 credit`) |
| `model_pricing.input_price_per_1k`, `model_pricing.output_price_per_1k` | Microcredits per 1,000 tokens |

Additional details:

- `grant_purchase` uses the caller-supplied `idempotency_key` verbatim as the
  ledger key. A replay with the same `(user_id, amount_microcredits,
  reference_id)` fingerprint returns the original entry; any different
  fingerprinted field returns `409 idempotency_conflict`. A different key is a
  new grant even when the reference is unchanged.
- Reservation `request_id` values are unique per user (migration 0004 replaced
  the global constraint with `(user_id, request_id)`). A replay keeps the
  original expiry: `expires_in_seconds` is not fingerprinted. The TTL is capped
  at 86400 seconds (1 day, `422 invalid_ttl` above it); monthly spend caps use
  UTC calendar-month boundaries. Money totals that would exceed the BIGINT
  range are rejected with `422 amount_too_large` before any row is mutated.
- `credit_ledger` is append-only; the migration installs a trigger that rejects
  `UPDATE` and `DELETE`. `credit_balances` is a materialised projection and is
  rebuildable by replaying the ledger.
- `api_keys.prefix` stores exactly the first 8 characters of the key's random
  part (the characters after `sk_live_`); responses render it as
  `sk_live_XXXXXXXX`. `key_hash` stores the hex SHA-256 digest of the full key
  plus `API_KEY_PEPPER`. Raw keys are never stored.
- `users.password_hash` stores an Argon2id hash for local email/password
  accounts; plaintext passwords are never stored, returned, or logged.
- `audit_logs` metadata must never contain prompts, API keys, or secrets.

## Endpoints

- `GET /healthz` — liveness, no dependencies.
- `GET /readyz` — readiness; checks Postgres (`SELECT 1`), Redis (`PING`), and
  the private LiteLLM readiness endpoint. Returns `503` if any check fails.

### Phase 2 authentication (`/auth`, browser/CORS-facing)

- `POST /auth/register` — `{email, password, display_name?}`; creates a local
  account (password 12–128 chars, Argon2id hash) and returns
  `{access_token, token_type, expires_in, user}` with status `201`. Duplicate
  email returns `409` with code `email_exists`.
- `POST /auth/login` — `{email, password}`; returns the same token envelope.
  Unknown emails, wrong passwords, and suspended accounts all return the
  same generic `401 invalid_credentials`, so login never reveals account
  existence or suspension state.
- `GET /auth/me` — requires `Authorization: Bearer <jwt>` and returns the
  current user. An authenticated suspended user still gets
  `403 user_suspended` (authenticated context, not a login oracle).

JWTs are HS256, required claims are `exp`/`iss`/`aud`, and access tokens also
carry `iat`, `jti`, and `type=access`. `expires_in` is
`JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60`.

### Phase 2 API keys (`/dashboard/keys`, JWT protected)

- `POST /dashboard/keys` — `{name, scopes?}`; returns
  `{id, name, prefix, key, scopes, last_used_at, revoked_at, created_at}` with
  status `201`; `last_used_at` and `revoked_at` are `null` at creation. `key`
  is the full secret and is returned exactly once. Allowed scopes are
  `chat:completions` and `models:read` (default: both); unknown scopes return
  `422`.
- `GET /dashboard/keys` — returns a JSON array of
  `{id, name, prefix, scopes, last_used_at, revoked_at, created_at}`; never
  includes the full key or hash.
- `DELETE /dashboard/keys/{id}` — owner-only revocation; `204`, idempotent,
  sets `revoked_at`, purges the Redis cache entry immediately, and writes an
  `api_key.revoke` audit record. Unknown keys and keys owned by another user
  both return `404 not_found` (no cross-user enumeration).

API keys use the format `sk_live_` + 43 base62 characters derived from 32
random bytes. Lookups cache the positive principal in Redis as
`apikey:<sha256hex>` for 60 seconds; Redis failures fail open to Postgres.
`last_used_at` is updated only on cache misses, so its granularity is
approximately the 60-second cache TTL. Keys belonging to suspended users
fail with the same `401 invalid_api_key` as unknown or revoked keys.
Creating a key writes an `api_key.create` audit record. Audit metadata contains
only non-secret fields (`name`, `scopes`).

CORS is restricted to the dashboard origin derived from `APP_URL`; `/auth` and
`/dashboard` are browser-facing, while the public `/v1` API is never
CORS-enabled.
