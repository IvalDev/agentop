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

- ``search_documents`` — ``EXTRACT`` / ``AUTO`` / viewer+: search over the
  caller's tenant chunks (``hybrid`` by default, or ``semantic`` /
  ``keyword``), returning hits with verifiable citations (``document_id`` +
  character offsets, acceptance criterion 3). Semantic ranking embeds the
  query with the :class:`~app.modules.tools.registry.ToolContext`'s
  provider. Without a provider, or when the embedding backend fails, the
  tool degrades to ``keyword`` ranking (logged) instead of failing the
  agent's run; the output's ``mode`` reports the ranking actually used.
"""

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.identity.models import UserRole
from app.modules.retrieval.embeddings import EmbeddingError
from app.modules.retrieval.service import search
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolSpec,
    register_default,
)
from packages.contracts.schemas import SearchHit

logger = logging.getLogger(__name__)

SEARCH_QUERY_MAX_LENGTH = 500
SEARCH_TOP_K_DEFAULT = 5
SEARCH_TOP_K_MAX = 20


class SearchDocumentsInput(BaseModel):
    """Typed, bounded input of ``search_documents`` (no tenant: it is implicit)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=SEARCH_QUERY_MAX_LENGTH,
        description="What to look for in the organization's documents.",
    )
    top_k: int = Field(
        default=SEARCH_TOP_K_DEFAULT,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        description="Maximum number of passages to return.",
    )
    mode: Literal["hybrid", "semantic", "keyword"] = Field(
        default="hybrid",
        description="Ranking: hybrid (default), semantic (meaning), or keyword (exact words).",
    )


class SearchDocumentsOutput(BaseModel):
    """Ranked passages, each a verifiable citation into its source document.

    ``mode`` is the ranking actually used (``keyword`` after a degradation).
    """

    hits: list[SearchHit]
    mode: Literal["hybrid", "semantic", "keyword"]


async def search_documents(
    context: ToolContext, validated_input: SearchDocumentsInput
) -> SearchDocumentsOutput:
    """Return the caller's tenant passages that best match the query."""
    query, top_k, mode = validated_input.query, validated_input.top_k, validated_input.mode
    if mode != "keyword":
        if context.embeddings is not None:
            try:
                hits = await search(
                    context.session, context.tenant_id, query, mode, top_k, context.embeddings
                )
                return SearchDocumentsOutput(hits=hits, mode=mode)
            except EmbeddingError:
                logger.warning("search_documents: embeddings failed; degrading to keyword search")
        else:
            logger.warning("search_documents: no embedding provider; using keyword search")
    hits = await search(context.session, context.tenant_id, query, "keyword", top_k, _NO_EMBEDDINGS)
    return SearchDocumentsOutput(hits=hits, mode="keyword")


class _NoEmbeddings:
    """Provider stand-in for keyword mode, which never embeds (fails closed)."""

    dimension: int = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("keyword search does not embed")


_NO_EMBEDDINGS = _NoEmbeddings()


SEARCH_DOCUMENTS = register_default(
    ToolSpec(
        name="search_documents",
        description=(
            "Search the organization's documents and return the most relevant "
            "passages, each numbered for citation."
        ),
        kind=ToolKind.EXTRACT,
        approval_policy=ApprovalPolicy.AUTO,
        required_role=UserRole.VIEWER,
        handler=search_documents,
        input_model=SearchDocumentsInput,
        output_model=SearchDocumentsOutput,
    )
)
