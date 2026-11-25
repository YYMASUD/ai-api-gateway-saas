# Security

## Scope and status

This document describes the Phase 1 security posture and the controls required before real user traffic. Phase 1 focuses on secret isolation, network boundaries, request tracing, redacted logging, and CI checks. Authentication, authorization, rate limiting, billing enforcement, and production monitoring are planned/later-phase work.

## Assets and threat model

Protected assets:

- Upstream provider credentials
- LiteLLM master key
- FastAPI API keys and dashboard JWTs
- API key pepper, JWT signing secret, Stripe secrets, email credentials
- Customer data, credit balances, usage records, and audit logs

Primary threats:

| Threat | Phase 1 control | TODO |
| --- | --- | --- |
| Secret in source/control history | Blank `.env.example`; `.gitignore` excludes `.env*`; gitleaks in CI; no secret defaults in settings | Secret-manager rotation automation |
| Provider key leaked to browser/web | Provider keys only injected into LiteLLM container; web env has public origins only | Enforce with policy/admission checks in deployment |
| Direct access to private services | No published ports for Postgres/Redis/LiteLLM/worker; single private network | Private VPC/subnet and security groups in Terraform |
| API key theft/leak | SHA-256 of `raw_key + API_KEY_PEPPER` in schema; helpers use constant-time comparison | Wire auth dependency; add rotation/revocation UI and rate limits |
| JWT forgery | HS256 pinned in helpers; required `exp`, `iss`, `aud` claims | Wire verification into dashboard routes; key rotation |
| Stripe webhook forgery | Env-only webhook secret placeholder | Verify signatures; idempotent event processing; replay protection |
| Sensitive log leakage | Structlog processor drops sensitive keys and masks known token formats | Add log-access controls and retention policy |
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

## Network and container boundaries

- `web` and `api` publish `127.0.0.1:3000` and `127.0.0.1:8000` respectively in local Compose. Publishing only on loopback avoids accidental LAN exposure.
- `postgres`, `redis`, `litellm`, and `worker` publish no host ports. They are reachable only from services on the `backend` Docker network.
- LiteLLM needs outbound internet access to providers; the local `backend` network is therefore not marked `internal`. In production, restrict egress with a firewall/proxy allowlist.
- The API Dockerfile runs as an unprivileged user and defines its own `/healthz` container healthcheck; the worker Dockerfile also runs unprivileged.
- Redis requires a password even on the private network. AOF persistence is enabled.
- Postgres is initialized with data checksums.

## Application controls implemented in Phase 1

- Pydantic settings read environment variables only; secret fields reject blank values.
- SQLAlchemy models never store raw API keys: only the 8-character `prefix` and `key_hash` are persisted.
- Credit/usage money values are signed `BIGINT` microcredits with non-negative balance checks, avoiding floating point accounting errors.
- Request IDs are sanitized (length/control-character checks), attached to responses, and bound into log context.
- CORS is applied to dashboard routes using an origin derived from `APP_URL`; the public `/v1` surface is not granted browser CORS.
- Security headers are set for all web routes: CSP, HSTS (non-production `max-age=0`), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, Permissions-Policy.
- LiteLLM config enables retries/timeouts and contains no keys; master key is read from `os.environ`.
- CI uses least-privilege `contents: read`, pinned action SHAs, `persist-credentials: false`, gitleaks, pip-audit, npm audit, and compose validation.

## Required before production traffic

1. Wire API-key auth into `/v1` and dashboard/admin routes; enforce per-key spend/rate limits.
2. Verify Stripe webhook signatures and make all event handling idempotent.
3. Add account-level authorization and audit-log writes for sensitive actions.
4. Run services behind TLS/private ingress; keep data services private with TLS and managed backups.
5. Add secret-manager injection and rotation runbooks; rotate any value that may have been exposed.
6. Enable metrics, traces, alerting, and retention with defined SLOs/on-call ownership.
7. Harden CSP with a nonce/hash strategy so `script-src 'unsafe-inline'` can be removed.
8. Pin all third-party images by digest (LiteLLM is pinned; Postgres/Redis tags should also be pinned) and scan images. Run containers with `read_only: true`, `cap_drop: ALL`, and `security_opt: ["no-new-privileges:true"]` where the image allows it; these are documented production requirements and are not set on the local dev stack.
9. Add dependency lockfiles/SBOMs, automated patch PRs, and release signing.
10. Perform an external security review and dependency/container scan before launch.

## Vulnerability and incident reporting

Do not open public issues for sensitive findings. Use the repository's private security advisory channel or contact the repository owner directly. During an incident: rotate exposed secrets first, preserve logs/evidence, then follow `docs/RUNBOOK.md`.
