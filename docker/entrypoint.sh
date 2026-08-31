#!/bin/sh
# Container entrypoint: apply DB migrations, then hand off to the CMD
# (uvicorn). The backend does not become "ready" (its /api/health check
# passes) until this has completed.
set -e

echo "[entrypoint] applying database migrations: alembic upgrade head"
alembic upgrade head
echo "[entrypoint] migrations up to date; starting: $*"

exec "$@"
