# Root entry point for Streamlit Cloud.
# Streamlit Cloud runs: streamlit run streamlit_app.py
# This delegates to asgi_app.py which mounts FastAPI at /api + Streamlit dashboard at /
import runpy, pathlib, sys  # noqa: E401

sys.path.insert(0, str(pathlib.Path(__file__).parent))
runpy.run_path(str(pathlib.Path(__file__).parent / "asgi_app.py"), run_name="__main__")
