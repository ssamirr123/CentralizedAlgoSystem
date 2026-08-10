"""
ASGI entry point for local development.

Architecture:
  /          → Streamlit dashboard (dashboard/streamlit_app.py)
  /api/...   → FastAPI backend (backend/main.py)

Run locally:
  streamlit run asgi_app.py

Streamlit Cloud runs streamlit_app.py instead, which duplicates this
construction rather than importing it — `streamlit run` only detects an
App instance when st.App(...) is called directly in the entry script.
"""
from __future__ import annotations

import pathlib

import streamlit as st
from starlette.routing import Mount

from backend.main import app as fastapi_app

_DASHBOARD = pathlib.Path(__file__).parent / "dashboard" / "streamlit_app.py"

app = st.App(
    str(_DASHBOARD),
    routes=[
        Mount("/api", app=fastapi_app),
    ],
)

if __name__ == "__main__":
    app.run()
