# Entry point for Streamlit Community Cloud.
# Streamlit Cloud looks for streamlit_app.py at the repo root.
# This file simply re-runs the actual dashboard from the dashboard/ folder.
import runpy, pathlib, sys  # noqa: E401

sys.path.insert(0, str(pathlib.Path(__file__).parent))
runpy.run_path(str(pathlib.Path(__file__).parent / "dashboard" / "streamlit_app.py"), run_name="__main__")
