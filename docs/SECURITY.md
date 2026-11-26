# Security

## Scope and status

This document describes the current security posture and the controls required before real user traffic.

- **Phase 1** focuses on secret isolation, network boundaries, request tracing, redacted logging, and CI checks.
- **Phase 2** adds self-built email/password authentication (Argon2id) and API-key lifecycle management in FastAPI.
- **Phase 3** adds the credit ledger service (row-locked, idempotent reserve/settle/refund/release_expired/grant_purchase) and owner-only dashboard credit reads.
- **Phase 4** wires the OpenAI-compatible `/v1` inference surface through stateless LiteLLM, adding API-key scope enforcement, request size caps, Redis-backed rate limiting (deliberately fail-open on Redis outage), and the reserve/settle/refund lifecycle end-to-end.

Stripe billing/webhooks, usage dashboards, and production monitoring remain later-phase work. The dashboard's interim client-side token handling (see [Interim dashboard token handling](#interim-dashboard-token-handling-sessionstorage)) is **not** production-acceptable and must be migrated.

## Assets and threat model

Protected assets:

- Upstream provider credentials
- LiteLLM master key
- FastAPI API keys and dashboard JWTs
- API key pepper, JWT signing secret, Stripe secrets, email credentials
- Customer data, credit balances, usage records, and audit logs

Primary threats:

| Threat | Control | TODO |
| --- | --- | --- |
| Secret in source/control history | Blank `.env.example`; `.gitignore` excludes `.env*`; gitleaks in CI; no secret defaults in settings | Secret-manager rotation automation |
| Provider key leaked to browser/web | Provider keys only injected into LiteLLM container; web env has public origins only | Enforce with policy/admission checks in deployment |
| Direct access to private services | No published ports for Postgres/Redis/LiteLLM/worker; single private network | Private VPC/subnet and security groups in Terraform |
| Credential stuffing / weak passwords | Argon2id hashing; 12–128 char policy; generic invalid-credential responses | Rate limiting, lockout/backoff, MFA, breached-password checks |
| API key theft/leak | `sk_live_` + 32 random bytes; SHA-256+pepper hash (raw never stored); 8-char prefix only; deterministic peppered-hash lookup (constant-time compare helper available); revoke invalidates Redis cache | Rate limits, per-key spend caps, rotation automation |
| JWT forgery/replay | HS256 pinned in code and config; required `exp`, `iss`, `aud`; short TTL via `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | Token revocation list, signing-key rotation, move browser tokens to HttpOnly cookies |
| Dashboard token exposure (XSS) | Interim `sessionStorage` storage — **accepted risk for Phase 2 only** | Migrate to HttpOnly cookies behind a BFF |
| Stripe webhook forgery | Env-only webhook secret placeholder | Verify signatures; idempotent event processing; replay protection |
| Sensitive log leakage | Structlog processor drops sensitive keys and masks known token formats; no key/hash/token logging | Add log-access controls and retention policy |
| Dependency vulnerabilities | pip-audit and npm audit in CI | Automated patching/SBOM and signed images |
| Request tracing gaps | Request ID on response and log context | Centralized log search and alerting |

## Secret handling

Rules:

1. **No secrets in files committed to git.** Use environment variables at runtime. `.env` is ignored; `.env.example` is blank and committed only as a name template.
2. **Production secrets come from a secrets manager.** The repository and images do not contain credentials. The deployment phase will inject values at runtime.
3. **Provider keys are LiteLLM-only.** Add the relevant env name to the LiteLLM service/deployment secret set and reference it as `os.environ/<NAME>` in `infra/litellm/config.yaml`. Never put a provider key in `apps/web`, `apps/api`, `apps/worker`, docs, or compose.
4. **LiteLLM master key is shared only with FastAPI** (and the LiteLLM container) for management/proxy auth. Next.js and the browser never receive it.
5. **Mandatory secrets have no defaults.** `DATABASE_URL`, `REDIS_URL`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `LITELLM_MASTER_KEY`, `JWT_SECRET`, and `API_KEY_PEPPER` must be non-empty or compose/config fails.
6. **Do not print secrets.** CI never echoes secret values. Application logging redacts fields such as `authorization`, `password`, `database_url`, `redis_url`, `litellm_master_key`, `api_key_pepper`, prompts, completions, and message bodies.

Local generation examples (values are generated in your shell; do not paste them into files):

```bash
openssl rand -hex 32   # JWT_SECRET
openssl rand -hex 32   # API_KEY_PEPPER
openssl rand -hex 32   # LITELLM_MASTER_KEY
openssl rand -hex 24   # POSTGRES_PASSWORD / REDIS_PASSWORD
```

## Authentication and API-key controls (Phase 2)

### Passwords

- Hashing: **Argon2id** via a vetted library; parameters are pinned in code (time_cost=3, memory_cost=64 MB, parallelism=4, 32-byte hash, 16-byte salt), suitable for interactive login. Raw passwords are never logged or stored.
- Policy: **12–128 characters**; email is normalised to lowercase and must be unique.
- **Generic invalid-credential responses.** `POST /auth/login` returns a single `401` body that does not reveal whether the email exists, the password was wrong, or the account is suspended. Registration conflict (`409`) is the only place email existence is surfaced, and only through account creation. Suspended accounts still receive `403 user_suspended` on already-authenticated routes such as `GET /auth/me`.
- Password reset / email verification are **not yet available** (later phase); treat accounts without recovery accordingly.

### JWTs

- **HS256 pinned** in both code (`apps/api/app/core/security.py`) and config; `JWT_ALGORITHM` rejects any value other than `HS256`. Verification passes an explicit `algorithms=["HS256"]` list, rejecting `alg=none` and algorithm confusion.
- Required claims: `exp`, `iss`, `aud` (plus `sub` = user ID). Issuer/audience are checked.
- Access-token TTL: `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, constrained to **5–1440 minutes** (default 60). Short TTL limits replay if a token leaks.
- Signing secret (`JWT_SECRET`) and pepper (`API_KEY_PEPPER`) are mandatory, env-only, and never logged.

### API keys

- Format: `sk_live_` + base62 encoding of **32 random bytes** from a CSPRNG.
- Storage: only `SHA-256(raw_key + API_KEY_PEPPER)` (hex) and the **8-character prefix** are persisted; raw keys are shown **once** at creation and never retrievable.
- Verification recomputes `SHA-256(raw_key + API_KEY_PEPPER)` and looks it up by indexed equality against `api_keys.key_hash`; a constant-time comparison helper (`verify_api_key`) exists for direct hash comparison.
- Scopes: explicit allowlist (`chat:completions`, `models:read`); unknown scopes are rejected with `422`. As of Phase 4 the `/v1` routes **enforce** these scopes per endpoint: `GET /v1/models` requires `models:read`, `POST /v1/chat/completions` requires `chat:completions`; a valid key missing the required scope gets `403 insufficient_scope`.
- Lookup cache: successful key lookups are cached in Redis for **60 seconds**. Revocation deletes the cache entry immediately; if that delete fails, the key stops working within the 60s TTL (stale-but-bounded). A cached principal for a subsequently suspended user can also continue to authenticate for at most this 60s window; suspension is not yet an immediate cache purge trigger. `last_used_at` is written on the DB-lookup (cache-miss) path, so its granularity is approximately the 60s cache TTL. API keys authenticate all `/v1` requests (Phase 4).

### API-key controls on `/v1` (Phase 4)

- **Scope enforcement.** Every `/v1` request authenticates with a hashed API key and requires the scope for that endpoint (`models:read` for `GET /v1/models`, `chat:completions` for `POST /v1/chat/completions`). Missing/invalid/revoked keys return `401 invalid_api_key`; a valid key without the required scope returns `403 insufficient_scope`. No privilege is inferred from key name or owner.
- **Request size cap.** Bodies larger than `MAX_REQUEST_BYTES` (default 1,048,576 bytes) are rejected with `413 request_too_large` **before** parsing, validation, or any credit reservation, bounding memory/CPU abuse. `max_tokens` is bounded by `MAX_OUTPUT_TOKENS` (default 4096) and returns `422 max_tokens_exceeded`.
- **Rate limiting.** Per-key Redis sliding window (`RATE_LIMIT_REQUESTS_PER_MINUTE`, default 60/min) plus a concurrent in-flight cap (`RATE_LIMIT_MAX_CONCURRENT`, default 8) bound cost and abuse. Breaches return `429 rate_limit_exceeded` with a `Retry-After` header.
- **Deliberate fail-open tradeoff.** If Redis is unavailable, rate limiting **fails open** (requests proceed) to keep inference available during a cache outage. This is an accepted availability-over-strictness decision: an attacker with a valid key could exceed nominal limits during the outage. PostgreSQL-backed credit checks, reservations, spend caps, and the size cap still apply, so financial exposure stays bounded. Do not claim full abuse protection during Redis degradation.
- **No prompt/completion content stored or logged.** Prompts, messages, and completions are never persisted in the application database or written to logs/usage events. `usage_events` stores only user/key/model IDs, token counts, cost, latency, and status. Structured logging redacts sensitive fields and known token formats, including message bodies. Only the streaming transport relays content between LiteLLM and the client.
- **Master key confinement.** `LITELLM_MASTER_KEY` is shared only with the `api` service and the LiteLLM container over the private network. It is never returned to clients, the browser, or `web`/`worker`, and is never inline in config or images.
- **Full refund on upstream failure.** A reservation is opened before forwarding (with a 1-microcredit floor even for zero estimates). If LiteLLM fails (`502 upstream_error`) or times out (`504 upstream_timeout`), the reservation is **fully refunded** via a zero-amount `refund` ledger row; credits are not consumed for work that did not complete. For streaming client disconnects, only tokens actually produced are charged (`usage_events.status = partial`).
- **Stateless LiteLLM.** No LiteLLM database, virtual keys, or LiteLLM-side spend state. FastAPI/PostgreSQL is the single authority for auth, billing, and spend caps, so there is no second credential or quota store to drift out of sync.

### Audit logging

- Key **create** and **revoke** actions write to `audit_logs` with actor, action, target type/ID, IP, and request context. The raw key, its hash, JWTs, and passwords are never written to audit or application logs.
- The logging processor redacts `authorization`, `password`, `api_key`, `token`, `key_hash`, and similar fields, and masks known token formats (including `sk_live_...`).

## Credit ledger integrity (Phase 3)

The credit service (`reserve`, `settle`, `refund`, `release_expired`, `grant_purchase`) protects balances against double-spend, lost updates, and tampering:

- **Append-only ledger.** `credit_ledger` is the source of truth: `balance = sum(ledger)`, `reserved = sum(open reservations)`, `available = balance - reserved`. A `BEFORE UPDATE OR DELETE` trigger (`trg_credit_ledger_append_only`) raises on any mutation, so corrections are compensating entries, never edits.
- **Integer-only money.** All credit values are signed `BIGINT` microcredits (`1,000,000 = 1 credit`); no floats or decimals are stored or returned. `credit_balances` has non-negative checks on `balance` and `reserved`; reservations check `amount > 0` and `0 ≤ cost ≤ amount`.
- **Row locking.** Every operation locks rows with `SELECT ... FOR UPDATE` in a fixed order — `users` → `credit_balances` → `credit_reservations` — so concurrent operations on the same account serialize without deadlocks.
- **Idempotency.** Purchases and other ledger entries use the caller-supplied `credit_ledger.idempotency_key` with a fingerprint of user, amount, and reference. Reservations use a composite `(user_id, request_id)` uniqueness constraint. Retries with the same key and fingerprinted parameters return the original outcome; reusing a key with different parameters is a `409 idempotency_conflict`. Reservation retries keep the original expiry; `expires_in_seconds` is not fingerprinted.
- **Monthly spend cap.** `users.spend_cap_monthly_microcredits` (BIGINT, default 0 = disabled) is enforced at reserve time against spend in the current **UTC calendar month**; exceeding it returns `402 monthly_spend_cap_exceeded`. Insufficient available credits returns `402 insufficient_credits`.
- **Reservation expiry.** Reservations default to a **15-minute** expiry. Settling an expired reservation returns `410 reservation_expired`; `release_expired` frees stale holds so reserved credits cannot be stranded.
- **Owner-only reads.** `GET /dashboard/credits` and `GET /dashboard/credits/ledger` require a valid JWT and only ever return the token subject's own data; foreign reservation IDs are indistinguishable from missing ones (`404`).

Reconciliation (comparing ledger sums to materialised balances) is **read-only**. Rebuilding `credit_balances` by replaying the ledger is **admin-only tooling in a later phase**; until then, never hand-edit balances or ledger rows — use compensating operations and follow `docs/RUNBOOK.md`.

## Interim dashboard token handling (sessionStorage)

The Phase 2 dashboard stores the Bearer JWT in **`sessionStorage`** (`apps/web/lib/auth-token.ts`) so client components can call `/auth/*` and `/dashboard/*` directly.

Risks and constraints:

- Any XSS on the dashboard origin can read `sessionStorage` and exfiltrate the token for its lifetime (bounded by the short access-token TTL).
- `sessionStorage` is per-tab and clears on tab close, which limits persistence but does not mitigate XSS.
- This is an **interim Phase 2 measure and is not acceptable for production.**

Required production migration:

1. Move token issuance/holding to the server: store the session in an **HttpOnly, Secure, SameSite** cookie set by a **Backend-for-Frontend (BFF)** (e.g. Next.js route handlers that call FastAPI server-side).
2. Stop returning the access token to browser JavaScript; keep CORS `allow_credentials` scoped to the BFF origin.
3. Add CSRF protection (double-submit or SameSite=strict with origin checks) for cookie-authenticated calls.
4. Tighten CSP and remove `unsafe-inline` so an XSS foothold is less likely even before migration.

## Network and container boundaries

- `web` and `api` publish `127.0.0.1:3000` and `127.0.0.1:8000` respectively in local Compose. Publishing only on loopback avoids accidental LAN exposure.
- `postgres`, `redis`, `litellm`, and `worker` publish no host ports. They are reachable only from services on the `backend` Docker network.
- LiteLLM needs outbound internet access to providers; the local `backend` network is therefore not marked `internal`. In production, restrict egress with a firewall/proxy allowlist.
- The API Dockerfile runs as an unprivileged user and defines its own `/healthz` container healthcheck; the worker Dockerfile also runs unprivileged.
- Redis requires a password even on the private network. AOF persistence is enabled. Phase 4 rate-limit state lives in Redis; its unavailability makes rate limiting fail open rather than blocking inference.
- Postgres is initialized with data checksums.

## Application controls implemented (Phases 1–4)

- Pydantic settings read environment variables only; secret fields reject blank values.
- Passwords are hashed with Argon2id; login returns generic invalid-credential `401` responses.
- JWTs are HS256-pinned with required `exp`/`iss`/`aud`; algorithm is validated at config load.
- SQLAlchemy models never store raw API keys: only the 8-character `prefix` and `key_hash` (SHA-256 + pepper) are persisted.
- API-key lookup uses a 60-second Redis cache; revocation deletes the cache entry immediately.
- `audit_logs` records key create/revoke with actor and request context; secrets are never written.
- Credit/usage money values are signed `BIGINT` microcredits with non-negative balance checks, avoiding floating point accounting errors.
- Request IDs are sanitized (length/control-character checks), attached to responses, and bound into log context.
- CORS is applied to all routes except the public `/v1` surface, using an origin derived from `APP_URL`.
- `/v1` enforces per-endpoint API-key scopes (`insufficient_scope`), a request body size cap (`request_too_large`), and Redis-backed per-key rate limits with `Retry-After`; rate limiting fails open on Redis outage by design while PostgreSQL credit controls remain authoritative.
- `/v1` never stores or logs prompt/completion content; `usage_events` records only token counts, cost, latency, and status.
- The reserve is fully refunded on `502 upstream_error` / `504 upstream_timeout`, so failed inference consumes no credits.
- Security headers are set for all web routes: CSP, HSTS (non-production `max-age=0`), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, Permissions-Policy.
- LiteLLM config enables retries/timeouts and contains no keys; master key is read from `os.environ`.
- CI uses least-privilege `contents: read`, pinned action SHAs, `persist-credentials: false`, gitleaks, pip-audit, npm audit, and compose validation.

## Required before production traffic

1. Validate and product-sign-off on upstream providers/models; keep `/v1` rate limits, size caps, and scope enforcement tuned for launch traffic (they are implemented in Phase 4 but need production tuning).
2. Migrate the dashboard from interim `sessionStorage` JWTs to HttpOnly cookies behind a BFF, and add CSRF protections.
3. Add password reset/recovery and email verification; add login rate limiting/lockout and breached-password checks.
4. Verify Stripe webhook signatures and make all event handling idempotent.
5. Add account-level authorization checks and expand audit-log coverage for sensitive actions.
6. Run services behind TLS/private ingress; keep data services private with TLS and managed backups.
7. Add secret-manager injection and rotation runbooks (including JWT signing-key rotation and API-key pepper rotation); rotate any value that may have been exposed.
8. Enable metrics, traces, alerting, and retention with defined SLOs/on-call ownership.
9. Harden CSP with a nonce/hash strategy so `script-src 'unsafe-inline'` can be removed.
10. Pin all third-party images by digest (LiteLLM is pinned; Postgres/Redis tags should also be pinned) and scan images. In production, also add runtime hardening where the image permits it (`cap_drop: ALL`, `security_opt: ["no-new-privileges:true"]`, and a read-only root filesystem with explicit writable mounts). These are deployment requirements and are not set on the local dev stack.
11. Add dependency lockfiles/SBOMs, automated patch PRs, and release signing.
12. Perform an external security review and dependency/container scan before launch.

## Vulnerability and incident reporting

Do not open public issues for sensitive findings. Use the repository's private security advisory channel or contact the repository owner directly. During an incident: rotate exposed secrets first, preserve logs/evidence, then follow `docs/RUNBOOK.md`.
