"""ASGI application entry point.

Creates the FastAPI application using the factory pattern.
Suitable for use with uvicorn or other ASGI servers.

  uvicorn main:app --host 0.0.0.0 --port 8000
"""

from backend.application.factory import create_application

app = create_application()
