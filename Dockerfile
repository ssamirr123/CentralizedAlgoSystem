# syntax=docker/dockerfile:1
#
# Backend image: FastAPI (trading.api.app.create_app) served by Uvicorn.
# The container runs `alembic upgrade head` before serving (see
# docker/entrypoint.sh). No secrets are baked in -- everything comes from
# the environment / an .env file at run time.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=docker

WORKDIR /app

# psycopg2-binary and every other runtime dep ship as wheels, so no
# system build toolchain is needed. Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Non-root runtime user.
RUN useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser . .
RUN chmod +x docker/entrypoint.sh

USER appuser

EXPOSE 8000

# entrypoint runs migrations, then exec's the CMD
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["uvicorn", "trading.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
