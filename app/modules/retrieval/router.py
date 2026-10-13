"""Search API: ``POST /api/v1/search`` over keyword/semantic/hybrid modes.

Authentication follows the documents router: ``Depends(get_current_user)``
for any authenticated role (viewer+), and the tenant always comes from
the token claims — never from the request body.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.identity.deps import CurrentUser, get_current_user
from app.modules.retrieval.embeddings import EmbeddingError, EmbeddingProvider
from app.modules.retrieval.service import search
from packages.contracts.schemas import SearchRequest, SearchResponse

router = APIRouter(prefix="/api/v1/search", tags=["search"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def get_embeddings(request: Request) -> EmbeddingProvider:
    """Return the embedding provider attached to ``app.state.embeddings``."""
    return request.app.state.embeddings


EmbeddingsDep = Annotated[EmbeddingProvider, Depends(get_embeddings)]


@router.post("", response_model=SearchResponse)
async def search_documents(
    body: SearchRequest,
    session: SessionDep,
    embeddings: EmbeddingsDep,
    user: CurrentUserDep,
) -> SearchResponse:
    """Return tenant-scoped ranked hits for the query (any authenticated role).

    Semantic and hybrid modes embed the query; when the embedding backend
    is unavailable the failure surfaces as 502 while keyword search keeps
    working.
    """
    try:
        hits = await search(
            session,
            user.tenant_id,
            body.query,
            body.mode,
            body.top_k,
            embeddings,
        )
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SearchResponse(query=body.query, mode=body.mode, hits=hits)
