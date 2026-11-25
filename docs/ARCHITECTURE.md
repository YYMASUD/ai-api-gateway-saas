# Architecture

## Scope and status

Phase 1 establishes a local, production-shaped topology and security boundaries. Phase 2 adds self-built email/password authentication and API-key lifecycle management in FastAPI. Service code for health/readiness, configuration, persistence models, security helpers, structured logging, auth, and key management is implemented; inference routing, billing flows, and observability are later phases.

This document describes the intended architecture and marks which parts are actually implemented.

## Components

| Service | Runtime | Ports | Responsibility |
| --- | --- | --- | --- |
| `web` | Next.js 15 (standalone) | `127.0.0.1:3000` only | Public pages and dashboard shell. Holds no provider keys and no private datastore credentials. |
| `api` | FastAPI / Python 3.12 | `127.0.0.1:8000` only | Public OpenAI-compatible gateway (planned), dashboard API (planned), health/readiness (implemented), auth/usage/billing orchestration. |
| `worker` | ARQ / Python 3.12 | none | Private async jobs: usage rollup, credit settlement, webhook processing, reconciliation, email (stubs in Phase 1). |
| `postgres` | PostgreSQL 16 | none | System of record for users, API keys, credits, usage, payments, webhooks, audit logs, and model pricing. |
| `redis` | Redis 7 (AOF) | none | Queue backend for ARQ; Phase 2 also uses it for the 60s API-key lookup cache. Rate-limit state comes later. AOF persistence volume is enabled. |
| `litellm` | LiteLLM proxy | none | Only outbound model gateway. Owns provider routing/fallbacks; master key required for management and proxy auth. |

Network: a single Docker bridge network named `backend`. Only `web` and `api` publish ports. In production, TLS termination and ingress are added in front of `web`/`api`; Postgres, Redis, LiteLLM, and the worker remain private with no public listeners.

## Trust boundaries

```text
Internet / client
    |
    | HTTP(S) at the edge (TLS termination is a later-phase responsibility)
    v
Browser -------- dashboard origin --------> FastAPI (/dashboard, /billing)
Public API client ------------------------> FastAPI (/v1, Bearer API key)
    |                                           |
    |                                           | private backend network only
    v                                           v
Next.js server (no secrets)              +----------------+
                                         | LiteLLM proxy  |
                                         | master key     |
                                         +----------------+
                                                 |
                                                 | HTTPS to provider
                                                 v
                                          upstream providers

FastAPI / worker ---- private ----> PostgreSQL 16 and Redis 7
```

Rules enforced by configuration:

1. The browser never receives `DATABASE_URL`, `REDIS_URL`, `LITELLM_MASTER_KEY`, or provider keys.
2. `web` does not reach Postgres, Redis, or LiteLLM. Its `NEXT_PUBLIC_*` values are public origins only.
3. `api` is the only service that holds `LITELLM_MASTER_KEY` and calls LiteLLM.
4. Provider keys are injected only into the LiteLLM container from a secrets manager and referenced by `os.environ/<NAME>` in `infra/litellm/config.yaml`.
5. `worker` has no published port and consumes only private queue/datastore URLs. In Phase 1 its job bodies are no-ops that return `{"status": "noop", "job": ...}`.

## Request flow (intended Phase 2+ inference)

1. Client sends an OpenAI-compatible request to `POST /v1/chat/completions` with a gateway API key.
2. FastAPI authenticates the key by hashing `raw_key + API_KEY_PEPPER` with SHA-256 and looking up `api_keys.key_hash` (helpers and the 60s Redis lookup cache exist as of Phase 2; the `/v1` route wiring arrives in a later phase).
3. FastAPI checks/reserves credits using the append-only `credit_ledger` and `credit_balances` tables. Money is integer **microcredits**: `1,000,000 microcredits = 1 credit`; no floats are stored.
4. FastAPI calls LiteLLM over the private network with the master key, forwarding only the model alias and sanitized request fields.
5. LiteLLM selects a configured provider deployment and calls the upstream provider. Provider credentials never leave LiteLLM.
6. FastAPI records a `usage_events` row (idempotent on `request_id`), settles the reservation, and returns an OpenAI-compatible response/error.
7. Background workers roll up usage, settle credits, process webhook events, and send email.

As of Phase 2, step 2 (key authentication primitives and caching) is implemented; steps 3–7 and the `/v1` route wiring remain contracts/TODOs. The implemented API surface is health/readiness plus authentication and API-key lifecycle (see `docs/API.md`).

## Data model (Phase 1 schema)

The initial Alembic revision creates:

- `users`: accounts, status, role, optional password hash, Stripe customer id.
- `api_keys`: name, prefix, `key_hash`, status, timestamps. Raw keys are never stored.
- `credit_ledger`: append-only movements with a signed `amount_microcredits`, a typed `type`, reference, and unique idempotency key.
- `credit_balances`: available/reserved/lifetime counters with non-negative checks.
- `usage_events`: per-request user/key/model/token/cost/latency/status with a unique `request_id`.
- `payments`: provider payment/checkout references and amounts.
- `webhook_events`: provider event id, payload, status, attempts, and errors.
- `audit_logs`: actor/action/resource metadata.
- `model_pricing`: active input/output prices per 1,000 tokens in microcredits.

Database access is async SQLAlchemy 2.x with `asyncpg`. Alembic uses the same `DATABASE_URL` from settings.

## Configuration and secrets

- All runtime configuration comes from environment variables; settings objects have no secret defaults.
- `.env.example` lists every supported variable name with a blank value. Copy it to `.env` for local work only.
- Mandatory compose substitutions (`:?`) fail fast if core secrets are unset/blank: database URL, Redis URL, Postgres credentials, Redis password, LiteLLM master key, JWT secret, and API key pepper.
- Production values come from the platform secrets manager. No deployed secret is read from a file in the repository.
- LiteLLM config is mounted read-only. It contains no provider keys, only `os.environ/...` references and routing/timeouts.
- Terraform and monitoring directories are placeholders; no backend, provider, alert route, or credential is committed.

## Observability (planned)

Phase 1 provides structured JSON logs with request IDs and recursive redaction of sensitive fields. Metrics/traces/alerts are TODO:

- Expose `/metrics` on the private API interface only.
- Scrape API/LiteLLM/Postgres/Redis exporters from a private Prometheus.
- Export OpenTelemetry traces from web/API/worker to a collector configured by `OTEL_*`.
- Route alerts to the on-call system via secret-managed webhooks.

`infra/monitoring` contains non-loaded skeleton configs only.

## Deployment topology (later phase)

```text
Edge/LB (TLS, WAF optional)
   |-> /            -> web service
   |-> /v1, /dashboard, /billing, /webhooks -> api service
Private subnet/VPC:
   api -> postgres (managed, private, encrypted, backed up)
   api -> redis (managed, private, AUTH/TLS)
   api -> litellm -> providers
   worker -> redis/postgres
Secrets manager injects env at runtime.
```

Terraform will own networking, managed data services, runtime services, IAM, remote encrypted state, and secret-manager entries. The current `infra/terraform` files intentionally declare no resources.

## Decisions in Phase 2

- Argon2id for password hashing; 12–128 character password policy; generic invalid-credential 401 responses on login to prevent email enumeration.
- HS256 JWTs pinned in code and config; required `exp`/`iss`/`aud`; short TTL via `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (5–1440 minutes).
- API keys are `sk_live_` + 32 random bytes; only `SHA-256(raw_key + API_KEY_PEPPER)` and the 8-character prefix are stored.
- Redis is the API-key lookup cache with a 60s TTL; revocation deletes the cache entry immediately (stale-but-bounded on failure).
- The dashboard stores the JWT in `sessionStorage` as an interim Phase 2 measure; production must migrate to HttpOnly cookies behind a BFF.

## Decisions in Phase 1

- One private network (`backend`) is enough for local topology; do not mark it `internal: true` because LiteLLM needs outbound access to upstream providers.
- Web/API host ports bind to loopback by default to avoid accidental LAN exposure.
- Postgres password/Redis password are required even locally; there are no well-known defaults.
- LiteLLM is pinned by version tag plus digest; provider/model choices remain an explicit later decision.
- Money is integer microcredits end-to-end to avoid floating point drift.
- CORS is derived from `APP_URL` and applied to dashboard routes only; the public `/v1` API is not browser-CORS-enabled.

## Later-phase TODOs

- Implement and test inference, auth dependencies, billing, and usage ingestion.
- Decide whether LiteLLM uses its own database for virtual keys/spend or remains stateless with FastAPI authoritative.
- Add migrations for any new tables and a backward-compatible migration policy.
- Add deployment manifests/Terraform, private ingress, TLS, and secret-manager wiring.
- Add metrics, dashboards, alert routing, SLOs, and runbooks for production incidents.
