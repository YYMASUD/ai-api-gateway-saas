# Architecture

## Scope and status

Phase 1 establishes a local, production-shaped topology and security boundaries. Phase 2 adds self-built email/password authentication and API-key lifecycle management in FastAPI. Phase 3 adds the credit ledger service (reserve/settle/refund/release_expired/grant_purchase) and owner-only dashboard credit reads. Phase 4 wires the OpenAI-compatible `/v1` inference surface (`GET /v1/models`, `POST /v1/chat/completions` with streaming and non-streaming) through LiteLLM, adding API-key scope enforcement, request size caps, Redis-backed rate limiting, and the reserve/settle/refund lifecycle end-to-end. Phase 5 adds Stripe Checkout top-ups (`POST /billing/checkout`) and the signature-verified webhook receiver (`POST /webhooks/stripe`); the API records and queues events, and the **worker grants credits** idempotently into the shared ledger. Service code for health/readiness, configuration, persistence models, security helpers, structured logging, auth, key management, the credit ledger, inference routing, and billing ingestion is implemented; receipt email (Phase 6), usage dashboards, and observability remain later phases. LiteLLM runs **stateless** (no database, no virtual keys); provider/model selection is still pending product sign-off.

This document describes the intended architecture and marks which parts are actually implemented.

## Components

| Service | Runtime | Ports | Responsibility |
| --- | --- | --- | --- |
| `web` | Next.js 15 (standalone) | `127.0.0.1:3000` only | Public pages and dashboard shell. Holds no provider keys and no private datastore credentials. |
| `api` | FastAPI / Python 3.12 | `127.0.0.1:8000` only | Public OpenAI-compatible gateway (`/v1` implemented in Phase 4), dashboard API and auth (implemented), health/readiness (implemented), Stripe Checkout + webhook ingestion (Phase 5), usage dashboards (planned). |
| `worker` | ARQ / Python 3.12 | none | Private async jobs: `process_webhook` credit grants (Phase 5), usage rollup, credit settlement, expiry sweeps, reconciliation, email (Phase 6). Installs the shared API package so it can call `CreditService` directly. |
| `postgres` | PostgreSQL 16 | none | System of record for users, API keys, credits, usage, payments, webhooks, audit logs, and model pricing. |
| `redis` | Redis 7 (AOF) | none | Queue backend for ARQ; API-key lookup cache (60s); Phase 4 rate-limit state (sliding window + concurrency counters, fails open on outage). AOF persistence volume is enabled. |
| `litellm` | LiteLLM proxy (stateless) | none | Only outbound model gateway. Owns provider routing/fallbacks; master key required for proxy auth. Runs **stateless** in Phase 4: no LiteLLM database, no virtual keys, no spend store. FastAPI is authoritative for billing. |

Network: a single Docker bridge network named `backend`. Only `web` and `api` publish ports. In production, TLS termination and ingress are added in front of `web`/`api`; Postgres, Redis, LiteLLM, and the worker remain private with no public listeners.

## Trust boundaries

```text
Internet / client
    |
    | HTTP(S) at the edge (TLS termination is a later-phase responsibility)
    v
Browser -------- dashboard origin --------> FastAPI (/dashboard, /billing)
Public API client ------------------------> FastAPI (/v1, Bearer API key)
Stripe (signature-signed webhook) --------> FastAPI (/webhooks/stripe, no JWT)
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

## Request flow (Phase 4 inference)

1. Client sends an OpenAI-compatible request to `POST /v1/chat/completions` with a gateway API key.
2. **Size cap and validation.** FastAPI rejects bodies above `MAX_REQUEST_BYTES` (default 1 MiB) with `413 request_too_large`, then validates the OpenAI request shape and enforces `max_tokens ≤ MAX_OUTPUT_TOKENS` (default 4096). The model must be an enabled `model_pricing` row (`404 model_not_found` otherwise).
3. **Authentication and scope.** FastAPI authenticates the key by hashing `raw_key + API_KEY_PEPPER` with SHA-256 and looking up `api_keys.key_hash` (helpers and the 60s Redis lookup cache exist as of Phase 2; the `/v1` route wiring is implemented in Phase 4). The key must carry `chat:completions` (`403 insufficient_scope` otherwise). `GET /v1/models` uses the same authentication with required scope `models:read`.
4. **Rate limit.** Redis enforces a per-key sliding window (`RATE_LIMIT_REQUESTS_PER_MINUTE`, default 60) and concurrent in-flight cap (`RATE_LIMIT_MAX_CONCURRENT`, default 8); a breach returns `429 rate_limit_exceeded` with `Retry-After`. Redis outage **fails open** so inference stays available; PostgreSQL credit controls still apply.
5. **Reserve.** FastAPI checks/reserves credits using the append-only `credit_ledger`, materialised `credit_balances`, and `credit_reservations`. Money is integer **microcredits**: `1,000,000 microcredits = 1 credit`; no floats are stored. A reserve opens a `RESERVATION_TTL_SECONDS`-lived reservation (default 900s) against `available = balance - reserved`, enforcing the per-user UTC-month spend cap; it writes a zero-amount `reservation` ledger row and increases only `reserved` (`balance` is unchanged). A **1-microcredit floor** applies even to a zero estimate so zero-priced models still exercise the lifecycle.
6. **Forward.** FastAPI calls stateless LiteLLM over the private network with the master key, forwarding the model alias and sanitized request fields. Streaming requests are proxied as unbuffered SSE.
7. **LiteLLM routing.** LiteLLM selects a configured provider deployment and calls the upstream provider. Provider credentials never leave LiteLLM. With the approved local-only `model_list: []`, there is no routable deployment: a real inference call returns `502 upstream_error` and FastAPI fully refunds the reservation (expected local behavior).
8. **Settle and record.** FastAPI records a `usage_events` row (idempotent on `request_id`), settles the reservation (closing it `settled`, debiting actual cost, freeing the unused difference), and returns an OpenAI-compatible response/error. On upstream failure (`502`/`504`) the reservation is refunded/released rather than settled. If a streaming client disconnects, only tokens actually produced are charged and usage status is `partial`.
9. Background workers roll up usage, release expired reservations (`release_expired`), process webhook events, and send email; worker job bodies and scheduling are later-phase wiring.

As of Phase 4, steps 2–8 are implemented for `/v1` (size caps, validation, auth/scope, rate limiting, reservations, LiteLLM forwarding, settlement/refund, and usage events). LiteLLM remains **stateless**: FastAPI/PostgreSQL is authoritative for auth, billing, spend caps, and usage; there is no LiteLLM database and no virtual keys. Provider/model selection is still **pending product sign-off**, so local dev keeps `model_list: []` and seeds one zero-priced `gateway-default` placeholder row. The implemented API surface is health/readiness, authentication, API-key lifecycle, owner-only dashboard credit reads, `/v1`, and (from Phase 5) Stripe Checkout/webhook ingestion (see `docs/API.md`). Usage dashboards and observability remain later-phase work.

## Payment flow (Phase 5)

Stripe is the only external inbound caller, and it calls a public endpoint: there is no JWT on `/webhooks/stripe`. The API verifies the signature and queues; the worker grants. FastAPI **never** writes a credit ledger row for a purchase inline.

```text
Browser / dashboard
    |  POST /billing/checkout  (JWT)
    v
FastAPI: validate pack + JWT -> create payments row (pending)
    |  Stripe SDK: Checkout Session with the pack's Price ID
    v
Stripe-hosted Checkout page  --user pays--> Stripe
    |
    |  POST /webhooks/stripe  (Stripe-Signature, raw body, no JWT)
    v
FastAPI: verify HMAC over raw bytes -> insert webhook_events
         (unique stripe_event_id) -> enqueue process_webhook -> 200 quickly
    |
    |  private Redis ARQ queue
    v
Worker: process_webhook(event_id)
    |  grant_purchase(idempotency_key = stripe:<session_id>)
    v
credit_ledger (append-only purchase row) + credit_balances
    +  payments.status = "completed", webhook_events.processed_at set
```

Flow details:

1. **Checkout:** `POST /billing/checkout` (JWT) validates the `pack_id` against the catalog and creates a `payments` row with `status = pending` before calling Stripe, so the payment can always be reconciled. It calls Stripe Checkout with the pack's configured Price ID, stores `stripe_session_id`, and returns `{checkout_url, session_id, amount_cents, credits, microcredits}`. Blank Stripe settings make the endpoint return `503 stripe_not_configured` (local-only default).
2. **Webhook:** Stripe calls `POST /webhooks/stripe` with no JWT. FastAPI reads the **raw body**, verifies the `Stripe-Signature` header with `STRIPE_WEBHOOK_SECRET`, and returns `400 invalid_signature` on failure. On success it records the event idempotently in `webhook_events` (unique `stripe_event_id`), commits the row, then enqueues `process_webhook` for `checkout.session.completed` / `checkout.session.async_payment_succeeded` and returns `200 {"status":"queued"}`. A replay of an **unprocessed** event re-enqueues for recovery; a processed replay returns `200 {"status":"already_processed"}` without enqueueing. The 200 is returned quickly — no credit grant happens in the request path.
3. **Worker grant:** the worker reads the stored event and payment from PostgreSQL and grants only a **paid, USD, catalog-matching** checkout. It validates payment ownership, amount, currency, session, and the exact `metadata.credits` value, then calls `grant_purchase` with `idempotency_key = stripe:<session_id>`. It marks `payments.status = "completed"`, writes `credits_granted`, and sets `webhook_events.processed_at`. Five deliveries still produce exactly one grant because the ledger idempotency key is unique; duplicate recovery jobs are safe.
4. **Ledger:** the grant is an append-only positive `purchase` row in `credit_ledger`; `credit_balances` is updated materially. The gateway's `/v1` reserve/settle/refund flow then spends those credits as usual. Receipt email is **deferred to Phase 6**.

## Shared API/worker package decision

The worker reuses the API's `CreditService` rather than reimplementing ledger rules: there is exactly one implementation of credit math, idempotency, and row locking. Both packages are installed into the same interpreter.

- The API keeps its `app` module name; the worker package is `worker_app` so the two never collide on import.
- The Compose `worker` build uses the **repository root** as its context (`dockerfile: apps/worker/Dockerfile`) so the image can `COPY apps/api` and pip-install it before the worker package. Pin sets are a superset in the worker so shared libraries resolve to identical versions.
- CI installs the API package first (`pip install -e ./apps/api` from the repo root), then the worker with its dev extras (`pip install -e ".[dev]"` from `apps/worker`) before ruff/mypy/pytest.
- The worker receives `DATABASE_URL` and `REDIS_URL` only. It does **not** receive `STRIPE_SECRET_KEY` or `STRIPE_WEBHOOK_SECRET`: it reads the already-recorded event from the DB and never calls Stripe. `EMAIL_*` is reserved for Phase 6.

## Data model (Phase 1–3 schema)

The Phase 1/2/3 Alembic revisions (`0001_initial`, `0002_user_password_hash`, `0003_credit_reservations`) create:

- `users`: accounts, status, role, optional password hash (added by the `0002_user_password_hash` revision), Stripe customer id, and `spend_cap_monthly_microcredits` (BIGINT, default 0 = disabled; added by `0003_credit_reservations`, enforced per user at reserve time over the UTC calendar month).
- `api_keys`: name, prefix, `key_hash`, scopes, revoked/last-used timestamps. Raw keys are never stored.
- `credit_ledger`: append-only movements with a signed `amount_microcredits`, a typed `type`, reference, and unique idempotency key. A database trigger forbids UPDATE/DELETE.
- `credit_balances`: materialised balance/reserved/lifetime counters with non-negative checks, fully rebuildable by replaying `credit_ledger`. `balance` is the total unspent wallet (equals the ledger sum) and includes open holds, so spendable funds are `available = balance - reserved`.
- `credit_reservations` (Phase 3): outstanding credit holds — `id`, `user_id`, `api_key_id`, `request_id` unique per `(user_id, request_id)`, `status` (`open`/`settled`/`released`), `amount_microcredits` (> 0), `cost_microcredits` (0 ≤ cost ≤ amount), `expires_at` (default 15 minutes), `settled_at`, `released_at`, `created_at`, `updated_at`. Indexed on `(status, expires_at)` for expiry sweeps and `(user_id, created_at)` for history.
- `usage_events`: per-request user/key/model/token/cost/latency/status with a unique `request_id`.
- `payments`: provider payment/checkout references and amounts. `amount_cents` is the fiat charge; `credits_granted` holds the microcredits granted; `status` (`pending` → `completed`/`failed`); `stripe_session_id` (unique) links a Checkout session to its grant; `stripe_payment_intent`, `currency`, and `metadata` (e.g. `pack_id`) support reconciliation.
- `webhook_events`: provider event ingestion log — `stripe_event_id` (unique, the replay guard), `type`, raw `payload` (JSONB; never logged), `processed_at` (terminal-success marker), `error`, and `attempts`. Rows are append-only by convention; do not delete them to retry an event.
- `audit_logs`: actor/action/resource metadata.
- `model_pricing`: active input/output prices per 1,000 tokens in microcredits. Local dev seeds one placeholder row `gateway-default` at 0 microcredits per 1k input/output tokens; `GET /v1/models` lists enabled rows.

Database access is async SQLAlchemy 2.x with `asyncpg`. Alembic uses the same `DATABASE_URL` from settings.

## Configuration and secrets

- All runtime configuration comes from environment variables; settings objects have no secret defaults.
- `.env.example` lists every supported variable name with a blank value. Copy it to `.env` for local work only.
- Mandatory compose substitutions (`:?`) fail fast if core secrets are unset/blank: database URL, Redis URL, Postgres credentials, Redis password, LiteLLM master key, JWT secret, and API key pepper.
- Stripe settings (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_10/50/100`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL`) are blank by default. Blank `STRIPE_SECRET_KEY` disables billing with `503`; blank `STRIPE_WEBHOOK_SECRET` rejects webhooks with `400 invalid_signature`. Production values come from the secrets manager. The worker receives no Stripe variables.
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

## Decisions in Phase 3

- Credits are modelled as an append-only ledger plus a separate `credit_reservations` table rather than mutating balance directly: `balance = sum(ledger)`, `reserved = sum(open reservations)`, `available = balance - reserved`.
- Reserve writes an open reservation plus a zero-amount `reservation` ledger row (only `reserved` increases); settle writes the negative settlement debit and releases the hold; refund and `release_expired` write zero-amount `refund` rows and free the hold without changing `balance`. This keeps the ledger immutable and auditable.
- All credit operations are idempotent on a caller-supplied key (unique `credit_ledger.idempotency_key` / `credit_reservations.request_id`), so client retries cannot double-charge.
- Transactions lock `users` → `credit_balances` → `credit_reservations` with `SELECT ... FOR UPDATE` in that fixed order to prevent deadlocks and lost updates.
- Default reservation expiry is 15 minutes, swept by the worker's `release_expired`.
- The monthly spend cap moved to `users.spend_cap_monthly_microcredits` (BIGINT, 0 = disabled) and is enforced on reserve over the UTC calendar month; `spend_cap_monthly_cents` remains the fiat/Stripe value.
- Dashboard credit reads are owner-only (JWT `sub`); admin reconciliation/rebuild tooling is deferred to a later phase.

## Decisions in Phase 4

- LiteLLM runs **stateless**: no `database_url`, no `store_model_in_db`, no virtual keys. FastAPI (with PostgreSQL) is authoritative for authentication, billing, spend caps, and usage metering.
- Local-only defaults: no real upstream provider is selected yet. `model_list` stays empty and local dev seeds one zero-priced `gateway-default` placeholder pricing row; real inference calls return `502 upstream_error` with a full reservation refund until a provider is approved.
- The `/v1` pipeline order is fixed: size cap → validation → rate limit → reserve → forward → settle/refund → `usage_events`.
- A 1-microcredit reservation floor applies even when the estimate is 0, so zero-priced models exercise the full reservation lifecycle; settlement charges the true cost and releases the hold.
- Rate limiting is Redis-backed per key (sliding window + concurrent in-flight cap) and deliberately **fails open** on Redis outage; PostgreSQL-backed credit checks still apply.
- Streaming is proxied as unbuffered SSE; a client disconnect charges only tokens actually produced (`usage_events.status = partial`).
- LiteLLM `request_timeout` (300s) matches the gateway `HTTP_READ_TIMEOUT` so the gateway owns the client-visible timeout.

## Decisions in Phase 5

- Stripe Checkout is used for credit top-ups. The `api` service is the only service with Stripe keys; the worker reads the recorded event and payment from PostgreSQL and never calls Stripe.
- The worker **grants credits**, not FastAPI. The webhook request verifies the signature against the raw body, records the event idempotently, enqueues `process_webhook`, and returns `200` fast; the grant is asynchronous.
- Replay defence is layered: unique `webhook_events.stripe_event_id` stops duplicate jobs, and the ledger's `idempotency_key = stripe:<session_id>` stops duplicate grants even on job retry. Five deliveries → one grant.
- Money conversion is integer-only: `$1 = 1 credit` and `1 cent = 10,000 microcredits` (e.g. `pack_10` = 1000 cents = 10,000,000 microcredits).
- The worker builds from the repository root and installs the API package so `CreditService` has one implementation; `app` (API) and `worker_app` (worker) are distinct module names.
- Local defaults are blank Stripe variables; unconfigured Checkout returns `503` and unconfigured webhooks return `400`. All tests use mocks — no live Stripe calls.
- Receipt email is deferred to Phase 6.

## Decisions in Phase 1

- One private network (`backend`) is enough for local topology; do not mark it `internal: true` because LiteLLM needs outbound access to upstream providers.
- Web/API host ports bind to loopback by default to avoid accidental LAN exposure.
- Postgres password/Redis password are required even locally; there are no well-known defaults.
- LiteLLM is pinned by version tag plus digest; provider/model choices remain an explicit later decision.
- Money is integer microcredits end-to-end to avoid floating point drift.
- CORS is derived from `APP_URL` and applied to dashboard routes only; the public `/v1` API is not browser-CORS-enabled.

## Later-phase TODOs

- Wire usage dashboards/rollups; Stripe billing/webhooks and worker webhook-processing grants are implemented in Phase 5.
- Select upstream providers/models and public aliases (pending product sign-off), then populate LiteLLM `model_list` and real `model_pricing` rows.
- Add migrations for any new tables and a backward-compatible migration policy.
- Add deployment manifests/Terraform, private ingress, TLS, and secret-manager wiring.
- Add metrics, dashboards, alert routing, SLOs, and runbooks for production incidents.
