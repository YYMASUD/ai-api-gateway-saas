# Security

## Scope and status

This document describes the current security posture and the controls required before real user traffic.

- **Phase 1** focuses on secret isolation, network boundaries, request tracing, redacted logging, and CI checks.
- **Phase 2** adds self-built email/password authentication (Argon2id) and API-key lifecycle management in FastAPI.

Rate limiting, billing enforcement, and production monitoring remain planned/later-phase work. The dashboard's interim client-side token handling (see [Interim dashboard token handling](#interim-dashboard-token-handling-sessionstorage)) is **not** production-acceptable and must be migrated.

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
| API key theft/leak | `sk_live_` + 32 random bytes; SHA-256+pepper hash (raw never stored); 8-char prefix only; constant-time compare; revoke invalidates Redis cache | Rate limits, per-key spend caps, rotation automation |
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

- Hashing: **Argon2id** via a vetted library; parameters use library defaults suitable for interactive login. Raw passwords are never logged or stored.
- Policy: **12–128 characters**; email is normalised to lowercase and must be unique.
- **Generic invalid-credential responses.** `POST /auth/login` returns a single `401` body that does not reveal whether the email exists or the password was wrong. Registration conflict (`409`) is the only place email existence is surfaced, and only through account creation.
- Password reset / email verification are **not yet available** (later phase); treat accounts without recovery accordingly.

### JWTs

- **HS256 pinned** in both code (`apps/api/app/core/security.py`) and config; `JWT_ALGORITHM` rejects any value other than `HS256`. Verification passes an explicit `algorithms=["HS256"]` list, rejecting `alg=none` and algorithm confusion.
- Required claims: `exp`, `iss`, `aud` (plus `sub` = user ID). Issuer/audience are checked.
- Access-token TTL: `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, constrained to **5–1440 minutes** (default 60). Short TTL limits replay if a token leaks.
- Signing secret (`JWT_SECRET`) and pepper (`API_KEY_PEPPER`) are mandatory, env-only, and never logged.

### API keys

- Format: `sk_live_` + base62 encoding of **32 random bytes** from a CSPRNG.
- Storage: only `SHA-256(raw_key + API_KEY_PEPPER)` (hex) and the **8-character prefix** are persisted; raw keys are shown **once** at creation and never retrievable.
- Verification uses a constant-time comparison of the recomputed hash.
- Scopes: explicit allowlist (`chat:completions`, `models:read`); unknown scopes are rejected with `422`.
- Lookup cache: successful key lookups are cached in Redis for **60 seconds**. Revocation deletes the cache entry immediately; if that delete fails, the key stops working within the 60s TTL (stale-but-bounded). API keys are only meaningful once `/v1` auth is wired (later phase).

### Audit logging

- Key **create** and **revoke** actions write to `audit_logs` with actor, action, target type/ID, IP, and request context. The raw key, its hash, JWTs, and passwords are never written to audit or application logs.
- The logging processor redacts `authorization`, `password`, `api_key`, `token`, `key_hash`, and similar fields, and masks known token formats (including `sk_live_...`).

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
- Redis requires a password even on the private network. AOF persistence is enabled.
- Postgres is initialized with data checksums.

## Application controls implemented (Phase 1 and Phase 2)

- Pydantic settings read environment variables only; secret fields reject blank values.
- Passwords are hashed with Argon2id; login returns generic invalid-credential `401` responses.
- JWTs are HS256-pinned with required `exp`/`iss`/`aud`; algorithm is validated at config load.
- SQLAlchemy models never store raw API keys: only the 8-character `prefix` and `key_hash` (SHA-256 + pepper) are persisted.
- API-key lookup uses a 60-second Redis cache; revocation deletes the cache entry immediately.
- `audit_logs` records key create/revoke with actor and request context; secrets are never written.
- Credit/usage money values are signed `BIGINT` microcredits with non-negative balance checks, avoiding floating point accounting errors.
- Request IDs are sanitized (length/control-character checks), attached to responses, and bound into log context.
- CORS is applied to all routes except the public `/v1` surface, using an origin derived from `APP_URL`.
- Security headers are set for all web routes: CSP, HSTS (non-production `max-age=0`), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, Permissions-Policy.
- LiteLLM config enables retries/timeouts and contains no keys; master key is read from `os.environ`.
- CI uses least-privilege `contents: read`, pinned action SHAs, `persist-credentials: false`, gitleaks, pip-audit, npm audit, and compose validation.

## Required before production traffic

1. Wire API-key auth into `/v1`; enforce per-key spend/rate limits and quota.
2. Migrate the dashboard from interim `sessionStorage` JWTs to HttpOnly cookies behind a BFF, and add CSRF protections.
3. Add password reset/recovery and email verification; add login rate limiting/lockout and breached-password checks.
4. Verify Stripe webhook signatures and make all event handling idempotent.
5. Add account-level authorization checks and expand audit-log coverage for sensitive actions.
6. Run services behind TLS/private ingress; keep data services private with TLS and managed backups.
7. Add secret-manager injection and rotation runbooks (including JWT signing-key rotation and API-key pepper rotation); rotate any value that may have been exposed.
8. Enable metrics, traces, alerting, and retention with defined SLOs/on-call ownership.
9. Harden CSP with a nonce/hash strategy so `script-src 'unsafe-inline'` can be removed.
10. Pin all third-party images by digest (LiteLLM is pinned; Postgres/Redis tags should also be pinned) and scan images. Run containers with `read_only: true`, `cap_drop: ALL`, and `security_opt: ["no-new-privileges:true"]` where the image allows it; these are documented production requirements and are not set on the local dev stack.
11. Add dependency lockfiles/SBOMs, automated patch PRs, and release signing.
12. Perform an external security review and dependency/container scan before launch.

## Vulnerability and incident reporting

Do not open public issues for sensitive findings. Use the repository's private security advisory channel or contact the repository owner directly. During an incident: rotate exposed secrets first, preserve logs/evidence, then follow `docs/RUNBOOK.md`.
