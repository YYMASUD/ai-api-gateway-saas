# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create an unprivileged runtime user.
RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid gateway --no-create-home --shell /usr/sbin/nologin gateway

WORKDIR /srv/api

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./

# Install runtime dependencies only: the optional "dev" extra (pytest, ruff,
# mypy) must never end up in the production image.
RUN pip install --no-cache-dir "." \
    && find /srv/api -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && chown -R gateway:gateway /srv/api

USER 10001:10001

ENV PORT=8000 \
    WORKERS=2 \
    FORWARDED_ALLOW_IPS=127.0.0.1

EXPOSE 8000

# The container is read-only-friendly: nothing is written at runtime.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# Trust proxy headers only from the explicitly configured ingress address(es).
# Set FORWARDED_ALLOW_IPS to the ingress CIDR in production; never use '*'.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS} --no-server-header --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
