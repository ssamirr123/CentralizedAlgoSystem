"""
Application entrypoint.

The FastAPI object is assembled by trading.api.app.create_app() (Stage 4).
The legacy heartbeat endpoints now live in trading/api/legacy.py (Stage 5)
and are included by the factory. This module stays as the import-stable
entrypoint -- `app` -- so api/index.py (`from backend.main import app`),
`uvicorn backend.main:app`, and the existing tests keep working unchanged.
"""
from __future__ import annotations

from trading.api.app import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
