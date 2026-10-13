"""Health check router."""

from fastapi import APIRouter, Request

from packages.contracts import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    """Return service liveness plus database ping status.

    The endpoint never fails because of the database: liveness stays ``ok``
    and the ``database`` field reports ``"error"`` when no database is
    attached or the ping fails.
    """
    db = getattr(request.app.state, "db", None)
    database = "error"
    if db is not None:
        database = "ok" if await db.ping() else "error"
    return HealthResponse(status="ok", database=database)
