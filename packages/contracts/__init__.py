"""Shared Pydantic contracts used across application modules."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness probe payload returned by ``GET /healthz``.

    ``database`` is ``"ok"`` when a database ping succeeds and ``"error"``
    when no database is attached or the ping fails; it never changes
    ``status``, which reports service liveness only.
    """

    status: str
    database: str = "error"
