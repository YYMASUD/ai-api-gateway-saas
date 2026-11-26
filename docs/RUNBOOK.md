# Runbook

## Scope and status

The stack is local-only. There is no production deployment, on-call rotation, or alert routing yet. This runbook covers local operations and the failure modes most likely while developing the stack. Phase 1 established the topology, health/readiness, and data layer; Phase 2 added email/password auth and API-key management; Phase 3 added the credit ledger service and dashboard credit reads. Later-phase tasks are marked TODO.

## Start and stop

Prerequisites: Docker Engine with Compose v2, a filled-in `.env`.

```bash
cp .env.example .env          # once, then fill values
make dev-up                   # docker compose up --build -d
make ps
make logs
make dev-down                 # keeps postgres_data and redis_data volumes
```

Apply schema changes:

```bash
make migrate                  # docker compose run --rm api alembic upgrade head
```

Check service health:

```bash
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/readyz
docker compose ps
docker compose logs --tail=200 api worker litellm postgres redis web
```

Expected Phase 1 behavior:

- `/healthz` returns `{"status":"ok"}` while the API process is running.
- `/readyz` returns 200 only when Postgres, Redis, and LiteLLM are reachable; otherwise 503 with per-dependency status.
- LiteLLM starts with an empty model list in Phase 1; liveness exists, but inference routes are not configured.
- Worker job stubs return `{"status":"noop", "job": ...}`; no credits move yet.

Expected Phase 2 behavior:

- `POST /auth/register` returns 201 with a `TokenResponse` envelope (`access_token`, `token_type`, `expires_in`, `user`). Re-registering the same email returns 409.
- `POST /auth/login` returns 200 with the same envelope on success, or a generic 401 for any invalid-credential case (no enumeration of whether the email exists).
- `GET /auth/me` returns 200 with the `UserResponse` for the JWT's `sub`; 401 for missing/expired/invalid tokens.
- `GET /dashboard/keys` returns a bare JSON array of key metadata (no plaintext keys). `POST /dashboard/keys` returns the key exactly once in `CreatedApiKey.key`; `DELETE /dashboard/keys/{id}` returns 204 and invalidates the Redis cache entry.
- JWT `expires_in` equals `JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60` (default 3600s). The web UI stores the token in `sessionStorage` (interim; see SECURITY.md).

Expected Phase 3 behavior:

- `GET /dashboard/credits` returns 200 with `balance_microcredits`, `reserved_microcredits`, and `available_microcredits` for the JWT's owner (`available = balance - reserved`).
- `GET /dashboard/credits/ledger` returns 200 with a keyset-paginated envelope (`items`, `next_cursor`), newest first; pass `next_cursor` as `cursor` for the next page. Entries are append-only and never change.
- The credit service (reserve/settle/refund/release_expired/grant_purchase) is idempotent per key, locks rows in the order users → credit_balances → credit_reservations, and enforces the per-user UTC-month spend cap on reserve. Reservations expire after 15 minutes by default.

## Common failures

| Symptom | Cause | Action |
| --- | --- | --- |
| `docker compose config` fails with `required variable is not set` | Blank/missing mandatory env vars | Fill `.env`; never commit it. CI uses temporary fake values only for validation. |
| API container unhealthy | `/healthz` not responding | Check `docker compose logs api`; confirm port 8000 is in use only by this stack. |
| `/readyz` returns 503 for `litellm` | LiteLLM not started or master key mismatch | Check `docker compose logs litellm`; confirm `LITELLM_MASTER_KEY` matches API env; verify health endpoint `/health/liveliness`. |
| `/readyz` returns 503 for `redis` | Auth mismatch or Redis not healthy | Confirm `REDIS_URL` contains the same password as `REDIS_PASSWORD`; check `redis-cli` health via logs. |
| `/readyz` returns 503 for `database` | Postgres not ready or DB URL mismatch | Check `POSTGRES_*` and `DATABASE_URL` alignment; `docker compose logs postgres`; rerun `make migrate`. |
| Web build shows wrong API origin | `NEXT_PUBLIC_API_BASE_URL` is baked at build time | Set the public origin before `npm run build`/`docker compose build`; never use a secret there. |
| Web client health call 404 | `NEXT_PUBLIC_API_BASE_URL` points at the wrong origin | Set the public FastAPI origin before building; the client targets `/healthz`. |
| Migration fails | Database unreachable or no revision applied | Wait for Postgres health, check `DATABASE_URL`, then run `make migrate`. |
| Port conflict | Host 3000/8000 already used | Stop the conflicting process; Compose binds to `127.0.0.1` only. |

## Phase 2 authentication and key troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `POST /auth/register` returns 409 | Email already registered | Login instead, or register with a different address. There is no self-service password reset yet. |
| `POST /auth/register` returns 422 | Email format, or password outside 12–128 chars | Fix the input; pydantic returns the field location in the error body. |
| `POST /auth/login` returns 401 for a known account | Generic invalid-credential response (anti-enumeration) | Verify the password; if the user truly forgot, wait for a later-phase password-reset flow. Do not bypass by editing the DB directly. |
| `GET /auth/me` returns 401 | Missing `Authorization` header, expired JWT, or wrong `iss`/`aud` | Re-login to obtain a fresh token. `expires_in` matches `JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60` (default 3600s). |
| JWT accepted locally but rejected elsewhere | Different `JWT_ISSUER`/`JWT_AUDIENCE`/`JWT_SECRET` on the verifier | Ensure every service that validates tokens shares the same three values. HS256 is pinned; `JWT_ALGORITHM` rejects anything else. |
| Change to `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` has no effect on existing sessions | TTL is enforced by the `exp` claim baked into issued tokens | Existing tokens keep their original expiry. New logins pick up the new TTL. Range is 5–1440 minutes. |
| `/dashboard/keys*` returns 404 | Key ID does not exist or belongs to a different user (existence is deliberately not revealed) | Use the owning account; do not weaken the 404 behavior. |
| `POST /dashboard/keys` returns 422 | Blank/oversized name (>64 chars), unknown scope, or an empty scopes list | Use only `chat:completions` and/or `models:read`, with at least one scope. |
| Revoked key still authenticates for up to ~60s | Stale Redis lookup cache (bounded) | Confirm the `DELETE /dashboard/keys/{id}` returned 204 and that `api_keys.revoked_at` is set. Cache entries are deleted on revoke; if the delete failed, the key remains usable for at most the 60s TTL. Inference enforcement itself (`/v1/*`) arrives in a later phase. |
| Verifying revocation locally | `/v1` auth not yet wired | Check `audit_logs` for `api_key.revoke` rows, list keys via `GET /dashboard/keys` (revoked keys show `revoked_at`), or query Redis (`KEYS apikey:*`) to confirm the entry is gone. |
| Password reset needed for a local account | Not yet implemented | Create a second account via `/auth/register`, or (destructive) reset the local DB via `make migrate` after deleting the user row. Do not store plaintext passwords while working around this. |

### 401 vs 403 quick reference

- **401 Unauthorized** — the request has no valid credentials (missing/expired/malformed JWT, login failure, invalid/revoked API key). Re-authenticate.
- **403 Forbidden** — credentials are valid but the account is suspended on an authenticated route (`user_suspended`). Do not retry with the same token.

## Phase 3 credit troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| Reserve fails with `402 insufficient_credits` | `available = balance - reserved` is below the requested amount | Check `GET /dashboard/credits`. Open reservations hold credits until settled or released (15-minute default expiry). Top up via `grant_purchase` (local testing) or wait for holds to clear. Do not hand-edit balances. |
| Reserve fails with `402 monthly_spend_cap_exceeded` | Spend in the current UTC calendar month would exceed `users.spend_cap_monthly_microcredits` | `0` disables the cap. The window resets at the UTC month boundary, not local time. For local testing, adjust the per-user column deliberately; never bypass the check in code. |
| Settle/refund returns `404 reservation_not_found` | Reservation unknown, or it belongs to another user (existence is deliberately not revealed) | Use the owning account and the exact `request_id` used at reserve time. Do not weaken the 404 behavior. |
| Settle returns `410 reservation_expired` | The reservation passed `expires_at` (default 15 minutes) and can no longer be settled | The hold is freed via `release_expired`. Re-reserve with a new `request_id` if the work must be retried; do not extend or edit the old reservation. |
| Operation returns `409 idempotency_conflict` | An idempotency key was reused with different parameters | Retries must repeat the original key **and** parameters to get the original outcome; new work needs a new key. Never delete ledger rows to "fix" a conflict. |
| Open reservations pile up past `expires_at` | The worker `expire_reservations` job body is still a stub | Invoke the credit service's `release_expired` operation (service layer) in local testing; reserved credits are freed only when it runs. Worker scheduling is later-phase wiring. |

### Ledger vs balance consistency

`credit_balances` is materialised and must equal the append-only ledger; `reserved` must equal the sum of open reservations. Read-only check for one user (replace `<user-uuid>`):

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT
  (SELECT COALESCE(SUM(amount_microcredits), 0) FROM credit_ledger
    WHERE user_id = '<user-uuid>') AS ledger_balance,
  (SELECT balance FROM credit_balances WHERE user_id = '<user-uuid>') AS balance_row,
  (SELECT COALESCE(SUM(amount_microcredits), 0) FROM credit_reservations
    WHERE user_id = '<user-uuid>' AND status = 'open') AS open_reserved,
  (SELECT reserved FROM credit_balances WHERE user_id = '<user-uuid>') AS reserved_row;
"
```

- `ledger_balance` must equal `balance_row`, and `open_reserved` must equal `reserved_row`. Re-run outside busy traffic: in-flight transactions can show a transient difference.
- A persistent mismatch is an incident: preserve logs/evidence and follow the incident-response steps below. The ledger is append-only (a trigger rejects UPDATE/DELETE) — never edit ledger rows or balances by hand. Reconciliation is read-only; an admin-only rebuild (replaying the ledger into `credit_balances`) is a later-phase tool.

## Data and persistence

- Postgres data: named volume `postgres_data` at `/var/lib/postgresql/data`.
- Redis data: named volume `redis_data` at `/data`, with AOF (`appendonly yes`, `appendfsync everysec`).
- Volumes survive `make dev-down`. To reset local data (destructive), use `docker compose down -v` only when you intend to discard it.

Local backup example (credentials are read from your shell `.env`, not written into the command output):

```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > ai_gateway_local_dump.sql
```

Restore (destructive; use a fresh database):

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < ai_gateway_local_dump.sql
```

TODO for production: managed snapshots, tested point-in-time recovery, encrypted backups, and documented RPO/RTO.

## Secret rotation (local)

1. Stop dependent services (`make dev-down` or stop API/worker/litellm).
2. Generate a new value in your shell; update `.env` only.
3. Restart/rebuild affected services.
4. Verify `/healthz`, `/readyz`, and logs.

Notes:

- Rotating `API_KEY_PEPPER` invalidates previously hashed API keys; planned key migration/rotation is a later-phase requirement. Also flush the API-key lookup cache in Redis after rotation (the cached hashes reference the old pepper).
- Rotating `JWT_SECRET` invalidates existing dashboard JWTs; users must sign in again. Changing `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` affects only newly issued tokens.
- Rotating `LITELLM_MASTER_KEY` requires updating both API and LiteLLM env values and restarting both.
- Rotating `POSTGRES_PASSWORD`/`REDIS_PASSWORD` requires updating the DSNs too (`DATABASE_URL`, `REDIS_URL`).

If any secret may have leaked, rotate immediately and preserve evidence before deleting logs.

## Logs

Application logs are structured JSON. Sensitive keys and known token formats are redacted. Do not disable redaction processors. Use request IDs (`X-Request-ID`) to correlate API logs with client errors.

TODO: central log store, retention policy, alert rules, dashboards, and trace sampling configuration via `OTEL_*`.

## Incident response (Phase 1 placeholder)

1. Declare the incident and record start time.
2. Preserve logs and container state (`docker compose ps`, `docker compose logs`).
3. Rotate exposed secrets first; then stop affected services if needed.
4. Communicate through the repository owner; do not post sensitive details publicly.
5. Write a post-incident note with timeline, root cause, and follow-up actions.

Production on-call ownership, paging, and severity definitions are TODO.

## Later-phase runbook TODOs

- Managed database/Redis failover and restore procedures
- Deployment rollback procedure and health-gated rollout
- LiteLLM provider outage/fallback operations and model disablement
- Stripe webhook replay/reconciliation procedure
- Rate-limit/quota incident playbook
- Alert routing, SLOs, and dashboards
- Password reset / email verification recovery procedures
- Production session migration from `sessionStorage` to HttpOnly cookies via a BFF
