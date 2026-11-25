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

Optional: `ENVIRONMENT`, `LOG_LEVEL`, `APP_URL`, `API_BASE_URL`,
`LITELLM_BASE_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_ALGORITHM` (must be
`HS256`), `HTTP_CONNECT_TIMEOUT`, `HTTP_READ_TIMEOUT`,
`SPEND_ALERT_THRESHOLD_MICROCREDITS`.

## Local development

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app --reload
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

- `credit_ledger` is append-only; the migration installs a trigger that rejects
  `UPDATE` and `DELETE`. `credit_balances` is a materialised projection and is
  rebuildable by replaying the ledger.
- `api_keys.prefix` stores exactly the first 8 characters of the plaintext key;
  `key_hash` stores the hex SHA-256 digest of the key plus `API_KEY_PEPPER`.
  Raw keys are never stored.
- `audit_logs` metadata must never contain prompts, API keys, or secrets.

## Endpoints

- `GET /healthz` — liveness, no dependencies.
- `GET /readyz` — readiness; checks Postgres (`SELECT 1`), Redis (`PING`), and
  the private LiteLLM readiness endpoint. Returns `503` if any check fails.

CORS is restricted to the dashboard origin derived from `APP_URL`; the public
`/v1` API is never CORS-enabled.
