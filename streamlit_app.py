# Root entry point for Streamlit Cloud.
# Streamlit Cloud runs: streamlit run streamlit_app.py
#
# The backend (FastAPI) is deployed separately (e.g. on Vercel) — see
# api/index.py and vercel.json — because Streamlit Cloud's edge gates every
# request behind a browser-only session handshake that non-browser HTTP
# clients (like EC2 heartbeat workers) can never complete. This app is a
# pure frontend that talks to that backend over HTTPS (see API_BASE_URL in
# dashboard/streamlit_app.py).
#
# runpy re-executes the dashboard script fresh on every Streamlit rerun,
# unlike a plain import (which Python would cache after the first run).
import runpy
import pathlib

runpy.run_path(str(pathlib.Path(__file__).parent / "dashboard" / "streamlit_app.py"))
