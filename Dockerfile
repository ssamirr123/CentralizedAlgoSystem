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
# system build toolchain is needed. `git` is needed only to install the
# pinned TradingAgents package below (a `git+https://...@v0.5.0` URL --
# see requirements-ai-research.txt's own comment on why it is NOT
# installed from PyPI). Install deps first for layer caching.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-ai-research.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-ai-research.txt

# Non-root runtime user.
RUN useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser . .
RUN chmod +x docker/entrypoint.sh

USER appuser

EXPOSE 8000

# entrypoint runs migrations, then exec's the CMD
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["uvicorn", "trading.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
