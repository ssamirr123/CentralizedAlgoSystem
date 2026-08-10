# Root entry point for Streamlit Cloud.
# Streamlit Cloud runs: streamlit run streamlit_app.py
#
# Streamlit's CLI only detects an ASGI App instance when `st.App(...)` is
# called directly in the script passed to `streamlit run` — importing it
# from another module (e.g. `from asgi_app import app`) is NOT detected,
# so the /api Mount silently never gets registered. This must therefore
# duplicate asgi_app.py's construction rather than import it.
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
