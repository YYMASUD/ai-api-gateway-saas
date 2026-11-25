# Runbook

## Scope and status

Phase 1 is local-only. There is no production deployment, on-call rotation, or alert routing yet. This runbook covers local operations and the failure modes most likely while developing the stack. Later-phase tasks are marked TODO.

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

- Rotating `API_KEY_PEPPER` invalidates previously hashed API keys; planned key migration/rotation is a later-phase requirement.
- Rotating `JWT_SECRET` invalidates existing dashboard JWTs; users must sign in again.
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
