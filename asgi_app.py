"""
ASGI entry point for Streamlit Cloud.

Architecture:
  /          → Streamlit dashboard (dashboard/streamlit_app.py)
  /api/...   → FastAPI backend (backend/main.py)

Run locally:
  streamlit run asgi_app.py
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
