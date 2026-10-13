"""Built-in production tools, registered on ``default_registry`` at import.

Safety policy (acceptance criterion 5): a production tool is a narrow, typed
capability over a tenant-scoped application service. No built-in tool may
expose a shell, arbitrary SQL, or open filesystem access:

- inputs are closed Pydantic models (``extra="forbid"``) with bounded fields,
  so a caller (ultimately the LLM) can never smuggle in extra arguments such
  as a ``tenant_id``, a path, or a query fragment;
- handlers only call application services, and those services filter on the
  ``tenant_id`` the executor takes from the authenticated principal;
- the set of registered names is pinned by an allowlist test, so adding a
  tool is a deliberate, reviewed change.

Tools:

- ``search_documents`` — ``EXTRACT`` / ``AUTO`` / viewer+: keyword search over
  the caller's tenant chunks, returning hits with verifiable citations
  (``document_id`` + character offsets, acceptance criterion 3). The handler
  contract carries no embedding provider, so the tool ranks with the
  retrieval service's ``keyword`` mode; semantic/hybrid ranking through a
  tool needs a provider seam that the orchestrator (T9) owns.
"""

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import UserRole
from app.modules.retrieval.embeddings import EmbeddingError
from app.modules.retrieval.service import search
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolKind,
    ToolSpec,
    ToolUser,
    register_default,
)
from packages.contracts.schemas import SearchHit

SEARCH_QUERY_MAX_LENGTH = 500
SEARCH_TOP_K_DEFAULT = 5
SEARCH_TOP_K_MAX = 20


class SearchDocumentsInput(BaseModel):
    """Typed, bounded input of ``search_documents`` (no tenant: it is implicit)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=SEARCH_QUERY_MAX_LENGTH,
        description="Keywords to look for in the tenant's documents.",
    )
    top_k: int = Field(
        default=SEARCH_TOP_K_DEFAULT,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        description="Maximum number of passages to return.",
    )


class SearchDocumentsOutput(BaseModel):
    """Ranked passages, each a verifiable citation into its source document."""

    hits: list[SearchHit]


class _KeywordOnlyEmbeddings:
    """Embedding placeholder for keyword search: keyword mode never embeds.

    Fails closed instead of returning fake vectors if the mode ever changes
    without wiring a real provider.
    """

    dimension: int = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("search_documents is keyword-only; no embedding provider is wired")


async def search_documents(
    session: AsyncSession,
    tenant_id: int,
    user: ToolUser,
    validated_input: SearchDocumentsInput,
) -> SearchDocumentsOutput:
    """Return the caller's tenant passages that best match the keywords."""
    hits = await search(
        session,
        tenant_id,
        validated_input.query,
        "keyword",
        validated_input.top_k,
        _KeywordOnlyEmbeddings(),
    )
    return SearchDocumentsOutput(hits=hits)


SEARCH_DOCUMENTS = register_default(
    ToolSpec(
        name="search_documents",
        description=(
            "Search the organization's documents by keywords and return matching "
            "passages with citations (document id and character offsets)."
        ),
        kind=ToolKind.EXTRACT,
        approval_policy=ApprovalPolicy.AUTO,
        required_role=UserRole.VIEWER,
        handler=search_documents,
        input_model=SearchDocumentsInput,
        output_model=SearchDocumentsOutput,
    )
)
