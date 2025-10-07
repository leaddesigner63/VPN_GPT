"""Application entrypoint for running the FastAPI app with `uvicorn main:app`."""
from api.main import app

__all__ = ["app"]
