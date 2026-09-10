#!/bin/bash
set -e

# PUID / PGID — match the container user to the host user who owns the
# export directory. Run `id` on the Linux host to find the right values,
# then set them in .env. Defaults to 1001 if not provided.
PUID=${PUID:-1001}
PGID=${PGID:-1001}

if [ "$PGID" != "$(id -g central)" ]; then
  groupmod -g "$PGID" central
fi
if [ "$PUID" != "$(id -u central)" ]; then
  usermod -u "$PUID" central
fi

# Fix ownership of the default bind-mount directories so the (now correctly
# mapped) container user can always write to them even if Docker created them
# as root. Custom EXPORT_DIR mounts don't need this because the PUID already
# matches the host owner.
chown -R central:central /app/exports /app/backups 2>/dev/null || true

exec gosu central gunicorn app:app
