# ai-api-gateway-saas

Production-grade AI API gateway SaaS with credit billing, API keys, usage metering, and OpenAI-compatible routing.

This repository is in **Phase 2**: the Phase 1 infrastructure, CI, and documentation foundation plus application skeletons are in place, and Phase 2 adds self-built email/password authentication and API-key lifecycle management in FastAPI. Inference, billing, and production deployment are still **not implemented**, and the stack does not deploy anywhere yet.

## What is here

| Path | Purpose |
| --- | --- |
| `apps/web` | Next.js 15 dashboard/public site (owned by the web workstream) |
| `apps/api` | FastAPI public gateway and private orchestration API |
| `apps/worker` | Private ARQ background worker |
| `infra/litellm` | LiteLLM proxy configuration skeleton |
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
- Only FastAPI calls LiteLLM, using `LITELLM_MASTER_KEY` from the environment.
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

# Worker
cd apps/worker
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
mypy app
```

The Phase 2 web UI stores the dashboard JWT in `sessionStorage` so the keys page can call FastAPI directly. This is an interim measure; production must move to HttpOnly cookies behind a BFF (see [docs/SECURITY.md](docs/SECURITY.md)).

The web build reads `NEXT_PUBLIC_API_BASE_URL` at build time; it must be a public origin, never a credential. The local default is `http://localhost:8000`.

`apps/web/.npmrc` pins installs to `https://registry.npmjs.org/`, and CI rejects any `http://` or non-canonical `"resolved"` URL in `package-lock.json` before running `npm ci`.

## Phase 1 and Phase 2 status

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

Not yet implemented (later phases):

- Real OpenAI-compatible `/v1/chat/completions` and `/v1/models` behavior
- Rate limiting and quota enforcement for API keys
- Credit reservation/settlement, Stripe checkout/webhooks, usage ingestion, and email delivery
- LiteLLM virtual keys / database-backed spend tracking
- Production deployment, TLS/ingress, managed databases, secret-manager wiring, and monitoring stacks
- Durable audit export, on-call alert routing, and SLOs
- Migration from interim `sessionStorage` JWTs to HttpOnly cookie / BFF-based session handling
- Password reset and account recovery flows

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

- Which upstream model providers and public model aliases are approved for the first paid launch?
- Should LiteLLM be stateless (billing authoritative in FastAPI) or use its own database for virtual keys and spend logs?
- Pricing/credit-pack catalog and Stripe product/price model are not fixed.
- Email provider choice and deliverability/domain setup are not fixed.
- Observability backend, retention, alert routing, and on-call ownership are not fixed.
- Public rate-limit tiers, quota policy, and abuse controls need product sign-off.
- Timing/design of the production session migration (HttpOnly cookies via a Next.js BFF) and of password reset/email verification flows.
- The Next.js typed client targets `/healthz`, matching the FastAPI liveness endpoint.
