"""ASGI entry point for the Sentinel AI Platform."""

from backend.application.factory import create_application

app = create_application()
