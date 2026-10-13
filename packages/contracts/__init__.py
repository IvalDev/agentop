"""Shared Pydantic contracts used across application modules."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness probe payload returned by ``GET /healthz``."""

    status: str
