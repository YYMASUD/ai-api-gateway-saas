FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app

# Build/install as root, then hand ownership to the unprivileged runtime user.
# chmod is explicit so the image does not depend on host file modes.
RUN python -m pip install --disable-pip-version-check --no-cache-dir . \
    && groupadd --system --gid 10001 worker \
    && useradd --system --uid 10001 --gid worker --no-create-home --shell /usr/sbin/nologin worker \
    && chown -R 10001:10001 /app \
    && chmod -R a+rX /app

USER 10001:10001

# No EXPOSE: this worker is private-network only and opens no public ports.
# REDIS_URL and any other runtime configuration are injected by the orchestrator.
CMD ["arq", "app.settings.WorkerSettings"]
