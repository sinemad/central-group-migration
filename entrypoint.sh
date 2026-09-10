#!/bin/bash
set -e

# Fix ownership of bind-mounted volumes so the non-root container user
# can write to them regardless of who created the directories on the host.
# This runs as root; gunicorn is then exec'd as the 'central' user via gosu.
chown -R central:central /app/exports /app/backups 2>/dev/null || true

exec gosu central gunicorn app:app
