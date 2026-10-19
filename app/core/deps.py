"""FastAPI dependency that exposes application settings to route handlers."""

from fastapi import Request

from app.core.config import Settings


def get_settings(request: Request) -> Settings:
    """Return the settings instance attached to ``app.state`` at creation."""
    return request.app.state.settings
