# ai-api-gateway-saas

Production-grade AI API gateway SaaS with credit billing, API keys, usage metering, and OpenAI-compatible routing.

This repository is in **Phase 5**: the Phase 1 infrastructure/CI foundation, Phase 2 authentication and API keys, Phase 3 credit ledger, Phase 4 OpenAI-compatible `/v1` inference, and Phase 5 Stripe payments are implemented. Phase 5 adds `POST /billing/checkout` (Stripe Checkout top-ups, JWT required) and `POST /webhooks/stripe` (no auth, signature-verified against the raw body). The API verifies, records the event idempotently, and enqueues `process_webhook`; **the worker grants credits** idempotently into the shared ledger — FastAPI never grants inline. The approved ratio is **$1 = 1 credit** (1 cent = 10,000 microcredits). Receipt email is **deferred to Phase 6**. **Local defaults are local-only:** LiteLLM is stateless with an empty `model_list` and one zero-priced `gateway-default` placeholder pricing row, so real inference calls return `502 upstream_error` with a full reservation refund until a provider is approved; blank Stripe variables disable billing (`503` on checkout, `400` on unsigned webhooks). Production deployment is still **not implemented**, and the stack does not deploy anywhere yet.

## What is here

| Path | Purpose |
| --- | --- |
| `apps/web` | Next.js 15 dashboard/public site (owned by the web workstream) |
| `apps/api` | FastAPI public gateway and private orchestration API |
| `apps/worker` | Private ARQ background worker |
| `infra/litellm` | Stateless LiteLLM proxy configuration (empty `model_list` in local dev) |
| `infra/terraform` | Deployment-phase Terraform placeholders |
| `infra/monitoring` | Observability placeholders |
| `docs` | Architecture, security, runbook, and API contracts |
| `.github/workflows/ci.yml` | Lint/typecheck/test/build, secret scan, dependency audits, compose validation |

## Architecture summary

```text
Browser / API clients
        |                    |  (dashboard only)
        v                    v
+----------------+   +----------------+
|   Next.js web  |   |    FastAPI     |
|  :3000 (local) |   | :8000 (local)  |
+----------------+   +----------------+
        |                    |
        |                    +------> LiteLLM (private, :4000, master key)
        |                    |              |
        |                    |              +--> upstream model providers
        |                    +------> PostgreSQL 16 (private)
        |                    +------> Redis 7, AOF (private)
        |                    |
        +--------------------+------> FastAPI only (no DB/Redis/LiteLLM creds)

                     private Docker network: backend
```

Hard boundaries:

- Only `web` (3000) and `api` (8000) publish host ports; both bind to `127.0.0.1` in the local stack.
- `postgres`, `redis`, `litellm`, and `worker` have **no published ports**. There is exactly one network, `backend`.
- Next.js holds **no** provider keys and no Postgres/Redis/LiteLLM credentials. It talks to FastAPI only.
- Only FastAPI calls LiteLLM, using `LITELLM_MASTER_KEY` from the environment. LiteLLM runs **stateless** (no database, no virtual keys); FastAPI/PostgreSQL owns authentication, billing, spend caps, and usage metering.
- Upstream provider credentials are injected into the LiteLLM container from a secrets manager and referenced as `os.environ/<NAME>` in `infra/litellm/config.yaml`. They are never committed and never given to web/API/worker.
- All secrets come from environment variables or a secrets manager. `.env.example` is blank by design.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details and [docs/SECURITY.md](docs/SECURITY.md) for the threat model.

## Prerequisites

- Docker Engine 24+ with Compose v2
- Python 3.12 for host-side API/worker checks
- Node.js 22 and npm for host-side web checks
- Optional: `pytest`, `ruff`, `mypy` in a local virtualenv

## Local setup

1. Create the local environment file:

   ```bash
   cp .env.example .env
   ```

2. Fill every required value in `.env`. The compose file fails fast on blank mandatory secrets. Generate strong local-only values, for example:

   ```bash
   openssl rand -hex 32   # JWT_SECRET
   openssl rand -hex 32   # API_KEY_PEPPER
   openssl rand -hex 32   # LITELLM_MASTER_KEY
   openssl rand -hex 24   # POSTGRES_PASSWORD / REDIS_PASSWORD
   ```

   `DATABASE_URL` and `REDIS_URL` must point at the compose services, for example
   `postgresql+asyncpg://<user>:<password>@postgres:5432/<db>` and
   `redis://:<password>@redis:6379/0`. Use the same credentials as the
   `POSTGRES_*` and `REDIS_PASSWORD` values.

   In production, do not use a `.env` file: inject these values from the
   platform secrets manager.

3. Start the stack:

   ```bash
   make dev-up
   # or: docker compose up --build -d
   ```

4. Open:

   - Web: <http://localhost:3000>
   - Dashboard signup/login: <http://localhost:3000/signup> and <http://localhost:3000/login>
   - API docs (non-production): <http://localhost:8000/docs>
   - API liveness: <http://localhost:8000/healthz>
   - API readiness: <http://localhost:8000/readyz>

5. Apply database migrations:

   ```bash
   make migrate
   # or: docker compose run --rm api alembic upgrade head
   ```

6. Stop the stack (keep volumes):

   ```bash
   make dev-down
   ```

Data is stored in the named volumes `postgres_data` and `redis_data`. Redis AOF persistence is enabled. Do not use this local stack as a production deployment.

## Useful commands

```bash
make ps
make logs
make compose-config
make build
```

## Tests and checks

Run everything:

```bash
make test
make lint
make typecheck
```

Per application:

```bash
# API (includes Phase 2 auth/key unit tests)
cd apps/api
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
mypy app

# Web (includes login/signup/keys UI tests; e2e requires browsers, not installed in CI)
cd apps/web
npm ci
npm test
npm run lint
npm run typecheck
npm run build
# npm run e2e   # skipped in CI: browsers are not installed

# Worker (installs the shared API package first, then the worker with dev extras;
# worker_app imports CreditService from the API `app` package)
cd apps/worker
python -m pip install -e ../api
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
mypy worker_app
```

The Phase 2 web UI stores the dashboard JWT in `sessionStorage` so the keys page can call FastAPI directly. This is an interim measure; production must move to HttpOnly cookies behind a BFF (see [docs/SECURITY.md](docs/SECURITY.md)).

The web build reads `NEXT_PUBLIC_API_BASE_URL` at build time; it must be a public origin, never a credential. The local default is `http://localhost:8000`.

`apps/web/.npmrc` pins installs to `https://registry.npmjs.org/`, and CI rejects any `http://` or non-canonical `"resolved"` URL in `package-lock.json` before running `npm ci`.

## Phase 1–5 status

Implemented in Phase 1:

- Local compose topology with the required network/port isolation, health checks, volumes, and AOF persistence
- LiteLLM config skeleton with env-referenced credentials, timeouts, retries, and fallback placeholders
- FastAPI health/readiness endpoints, request-ID propagation, redacted structured logging, OpenAI-style error envelope, security helpers, SQLAlchemy models, and an initial Alembic migration
- Next.js app shell, dashboard layout, typed API client, and security headers
- Private ARQ worker skeleton with no-op job stubs
- CI for lint/typecheck/unit tests/build, gitleaks, pip-audit, npm audit, and docker compose validation
- Terraform/monitoring placeholders and Phase 1 documentation

Implemented in Phase 2:

- Self-built email/password authentication in FastAPI: `POST /auth/register`, `POST /auth/login`, `GET /auth/me`
- Argon2id password hashing; HS256 JWTs with `exp`/`iss`/`aud` claims and a configurable access-token TTL (`JWT_ACCESS_TOKEN_EXPIRE_MINUTES`)
- API-key lifecycle: `sk_live_` + base62 encoding of 32 random bytes, SHA-256 + pepper hash, 8-character stored prefix (rendered `sk_live_XXXXXXXX`), scopes (`chat:completions`, `models:read`), Redis 60s lookup cache, and revoke-based cache invalidation
- Dashboard endpoints `GET /dashboard/keys`, `POST /dashboard/keys`, `DELETE /dashboard/keys/{id}`
- Next.js login/signup pages and a keys UI that store the Bearer JWT in `sessionStorage` as an **interim Phase 2 measure** (see `docs/SECURITY.md`)

Implemented in Phase 3:

- Credit ledger service: append-only `credit_ledger` plus materialised `credit_balances` and `credit_reservations`; `reserve`/`settle`/`refund`/`release_expired`/`grant_purchase` with row locking, idempotency, and integer microcredits
- Owner-only dashboard credit reads: `GET /dashboard/credits`, `GET /dashboard/credits/ledger` (JWT `sub`)
- Per-user UTC-month spend cap (`users.spend_cap_monthly_microcredits`) enforced on reserve

Implemented in Phase 4:

- OpenAI-compatible `GET /v1/models` (scope `models:read`; lists enabled `model_pricing` rows, no credit reservation) and `POST /v1/chat/completions` (scope `chat:completions`; streaming and non-streaming) — API-key auth
- Fixed pipeline: size cap → validation → rate limit → reserve → forward to LiteLLM → settle/refund → `usage_events`
- Request size cap (`MAX_REQUEST_BYTES`), output-token cap (`MAX_OUTPUT_TOKENS`), and Redis-backed per-key rate limiting (`RATE_LIMIT_REQUESTS_PER_MINUTE` sliding window + `RATE_LIMIT_MAX_CONCURRENT` in-flight cap; fails open on Redis outage; `429` with `Retry-After`)
- Reservation floor of 1 microcredit even for zero-priced models; full refund on `502 upstream_error` / `504 upstream_timeout`; partial charging on client disconnect
- Stateless LiteLLM: no database, no virtual keys; FastAPI/PostgreSQL authoritative for auth/billing/spend caps
- **Local-only defaults:** empty LiteLLM `model_list` and one zero-priced `gateway-default` placeholder; real inference returns `502 upstream_error` (fully refunded) until a provider is approved

Implemented in Phase 5:

- Stripe Checkout top-ups: `POST /billing/checkout` (JWT required) creates a session for `pack_10`/`pack_50`/`pack_100`, returning `201 {checkout_url, session_id, amount_cents, credits, microcredits}`; `503 stripe_not_configured` when Stripe is blank
- `POST /webhooks/stripe` is unauthenticated but signature-verified against the **raw body**; returns `200 {"status":"queued"}`, `200 {"status":"already_processed"}` on replay, or `400 invalid_signature`; it grants nothing itself
- The private worker's `process_webhook` grants credits idempotently (`stripe:<session_id>`), marks the payment `completed`, and sets `webhook_events.processed_at`; five deliveries → one grant
- Stripe secrets and Price IDs are `api`-only; the worker reads the database and has no Stripe keys. All Stripe variables are blank by default; all tests are mocked
- Credit math: **$1 = 1 credit**, 1 cent = 10,000 microcredits (see the flagged pack-table discrepancy in `docs/API.md`); receipt email deferred to Phase 6

Not yet implemented (later phases):

- Real upstream providers/models and public aliases (pending product sign-off); local dev uses only the zero-priced `gateway-default` placeholder and an empty LiteLLM `model_list`, so real inference returns `502 upstream_error` with a full reservation refund
- Billing dashboard and usage dashboards/rollups
- Production deployment, TLS/ingress, managed databases, secret-manager wiring, and monitoring stacks
- Durable audit export, on-call alert routing, and SLOs
- Migration from interim `sessionStorage` JWTs to HttpOnly cookie / BFF-based session handling
- Password reset and account recovery flows
- Receipt email (Phase 6) and remaining worker job bodies/scheduling for usage rollups and expiry sweeps (webhook credit grants are implemented)

See [docs/API.md](docs/API.md) for the implemented vs. planned API surface and [docs/SECURITY.md](docs/SECURITY.md) for the auth/key threat model.

## Assumptions and open questions

Assumptions:

- Local development uses Docker Compose; production will use managed Postgres/Redis and a secrets manager.
- Python 3.12 and Node.js 22 are the supported runtimes.
- The API uses async SQLAlchemy with `asyncpg`; `DATABASE_URL` should use the `postgresql+asyncpg://` driver.
- Redis requires a password even on the private network. Keep `REDIS_URL` and `REDIS_PASSWORD` consistent.
- CI uses npm and expects `apps/web/package-lock.json`; run `npm install` once if the lockfile is missing.
- Public web/API host ports bind to loopback for safe local defaults. A reverse proxy will terminate TLS in deployment.
- `make migrate` runs Alembic inside the API image; migration ownership stays with the API workstream.

Open questions:

- Which upstream model providers and public model aliases are approved for the first paid launch? (LiteLLM stays stateless either way; provider/model selection is pending product sign-off.)
- Decided: LiteLLM is stateless — no LiteLLM database/virtual keys; FastAPI is authoritative for billing.
- Pricing/credit-pack catalog and Stripe Price IDs are fixed for Phase 5 (`pack_10/50/100`; see the flagged microcredit discrepancy in `docs/API.md`); public pricing and launch currency still need product sign-off
- Email provider choice and deliverability/domain setup are not fixed.
- Observability backend, retention, alert routing, and on-call ownership are not fixed.
- Public rate-limit tiers, quota policy, and abuse controls need product sign-off.
- Timing/design of the production session migration (HttpOnly cookies via a Next.js BFF) and of password reset/email verification flows.
- The Next.js typed client targets `/healthz`, matching the FastAPI liveness endpoint.
