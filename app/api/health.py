"""Health check router."""

from fastapi import APIRouter

from packages.contracts import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Return service liveness status."""
    return HealthResponse(status="ok")
