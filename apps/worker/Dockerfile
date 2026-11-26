# Private worker image for the AI API gateway.
#
# BUILD CONTEXT: the REPOSITORY ROOT, not apps/worker, so the image can install
# the shared API package (the single source of ledger truth):
#
#   docker build -f apps/worker/Dockerfile -t ai-api-gateway-saas/worker:local .
#
# The API is installed first (from /opt/api-src) so its runtime pins resolve
# once, then the worker (whose pins are a superset of the API's) reuses the same
# shared libraries. The API package keeps its ``app`` module name; the worker is
# ``worker_app`` so the two never collide. The API ``[dev]`` extra is never
# installed.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY apps/worker/pyproject.toml ./
COPY apps/api /opt/api-src
COPY apps/worker/worker_app ./worker_app

# Install the API first, then the worker. Both are pinned so resolution is
# deterministic; installing the API first means shared pins are settled once.
RUN python -m pip install --disable-pip-version-check --no-cache-dir /opt/api-src \
    && python -m pip install --disable-pip-version-check --no-cache-dir .

# Build/install as root, then hand ownership to the unprivileged runtime user.
# chmod is explicit so the image does not depend on host file modes.
RUN groupadd --system --gid 10001 worker \
    && useradd --system --uid 10001 --gid worker --no-create-home --shell /usr/sbin/nologin worker \
    && chown -R 10001:10001 /app \
    && chmod -R a+rX /app

USER 10001:10001

# No EXPOSE: this worker is private-network only and opens no public ports.
# REDIS_URL and DATABASE_URL are injected by the orchestrator.
CMD ["arq", "worker_app.settings.WorkerSettings"]
