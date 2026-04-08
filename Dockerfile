# ── Build stage ───────────────────────────────────────────────────────────────
# Separate build stage keeps the final image clean — pip cache and
# build tools don't end up in the runtime layer.
FROM python:3.12-slim AS builder

WORKDIR /build

# Install deps into an isolated prefix so we can COPY just that tree
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="central-group-migration" \
      org.opencontainers.image.description="HPE Aruba Central group export/import tool" \
      org.opencontainers.image.source="https://github.com/aruba/pycentral"

# Non-root user — good practice for any network-facing service
RUN groupadd --gid 1001 central && \
    useradd  --uid 1001 --gid central --shell /bin/bash --create-home central

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code
COPY app.py         .
COPY exporters.py   .
COPY templates/     templates/

# The exports directory is mounted as a volume at runtime.
# Create it here so the directory exists even without a mount,
# and ensure the non-root user owns it.
RUN mkdir -p /app/exports && chown -R central:central /app

USER central

# gunicorn config:
#   -w 1              Single worker — _progress_queues is in-process state
#                     shared between SSE producer threads and subscriber
#                     threads. Multiple workers would each have their own
#                     dict, breaking SSE delivery 50% of the time.
#   --threads 8       8 threads handles concurrent export/import ops +
#                     SSE subscribers without blocking.
#   --timeout 120     SSE streams are long-lived; default 30s would kill them.
#   --keep-alive 5    Prevent premature SSE connection drops.
ENV GUNICORN_CMD_ARGS="--bind=0.0.0.0:8000 --workers=1 --threads=8 --timeout=120 --keep-alive=5 --access-logfile=- --error-logfile=-"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["gunicorn", "app:app"]
