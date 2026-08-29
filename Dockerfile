# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv, for the same install path the project uses everywhere else.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency metadata first, so a source edit does not re-resolve the world.
COPY pyproject.toml README.md ./
COPY src ./src

# Not editable: an image is a build artefact, not a working copy.
RUN uv pip install --system --no-cache ".[anthropic,openai,redis]"

# The database lives on a volume, not in the image layer. Losing the ledger on
# a redeploy would lose the billing history, which is the one thing here that
# cannot be recomputed.
ENV STORMDOOR_DB_PATH=/data/stormdoor.db \
    STORMDOOR_HOST=0.0.0.0 \
    STORMDOOR_PORT=8080
VOLUME ["/data"]

# Never run the gateway as root. It holds provider credentials.
RUN useradd --system --create-home --uid 10001 stormdoor \
    && mkdir -p /data && chown stormdoor:stormdoor /data
USER stormdoor

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "stormdoor.app:app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
