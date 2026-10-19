"""Tenant-scoped search: keyword, semantic, and hybrid (RRF) modes.

Ranking strategies (documented decisions for T5):

- **keyword** — Python-side scoring, identical on SQLite and PostgreSQL:
  the query is split on whitespace and lowercased; each term contributes
  its case-insensitive occurrence count in ``content`` (``str.count``).
  Counts are summed; chunks scoring zero are dropped, so a query with no
  matches returns nothing instead of filler.
- **semantic** — the query is embedded, then chunks are ranked by cosine
  similarity. On PostgreSQL the distance is computed by pgvector
  (``CAST(embedding AS vector) <=> :query`` ordered ascending); on every
  other dialect (the SQLite test suite) tenant chunks with non-null
  embeddings are fetched and scored in Python. Only chunks with a
  non-null embedding participate.
- **hybrid** — Reciprocal Rank Fusion over the two rankings:
  ``score = Σ 1/(60 + rank)`` with 1-based ranks (RRF k = 60). Each
  component list is truncated to ``top_k`` before fusion, and chunks
  present in only one list still appear.

Every query filters on ``tenant_id`` (the same rule as identity and
documents). Hits carry ``document_id`` and character offsets so each
citation is verifiable against the source document.
"""

import math
from uuid import UUID

from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.documents.models import Document, DocumentChunk
from app.modules.retrieval.embeddings import EmbeddingError, EmbeddingProvider
from packages.contracts.schemas import SearchHit

RRF_K = 60
_SEARCH_MODES = frozenset({"semantic", "hybrid", "keyword"})

RankedChunk = tuple[DocumentChunk, float]


async def search(
    session: AsyncSession,
    tenant_id: int,
    query: str,
    mode: str,
    top_k: int,
    embeddings: EmbeddingProvider,
) -> list[SearchHit]:
    """Return the top ``top_k`` chunks of ``tenant_id`` for ``query``.

    Raises ``ValueError`` for an unknown ``mode`` and ``EmbeddingError``
    when embedding the query fails (semantic/hybrid only).
    """
    if mode not in _SEARCH_MODES:
        raise ValueError(
            f"invalid search mode {mode!r}; expected one of 'semantic', 'hybrid', 'keyword'"
        )

    filenames = await _document_filenames(session, tenant_id)

    if mode == "keyword":
        chunks = await _fetch_chunks(session, tenant_id)
        ranked = _keyword_scores(chunks, query)[:top_k]
    elif mode == "semantic":
        ranked = await _semantic_scores(session, tenant_id, query, embeddings, top_k)
    else:  # hybrid: fuse both rankings with Reciprocal Rank Fusion
        chunks = await _fetch_chunks(session, tenant_id)
        keyword_ranked = _keyword_scores(chunks, query)[:top_k]
        semantic_ranked = await _semantic_scores(session, tenant_id, query, embeddings, top_k)
        ranked = _reciprocal_rank_fusion(keyword_ranked, semantic_ranked, top_k)

    return _build_hits(ranked, filenames)


async def _document_filenames(session: AsyncSession, tenant_id: int) -> dict[UUID, str]:
    """Map tenant document ids to filenames for citation payloads."""
    rows = await session.execute(
        select(Document.id, Document.filename).where(Document.tenant_id == tenant_id)
    )
    return {document_id: filename for document_id, filename in rows.all()}


async def _fetch_chunks(session: AsyncSession, tenant_id: int) -> list[DocumentChunk]:
    """All chunks of one tenant ordered deterministically per document."""
    result = await session.execute(
        select(DocumentChunk)
        .where(DocumentChunk.tenant_id == tenant_id)
        .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
    )
    return list(result.scalars())


def _keyword_scores(chunks: list[DocumentChunk], query: str) -> list[RankedChunk]:
    """Score chunks by summed case-insensitive query-term occurrence counts."""
    terms = [term for term in query.lower().split() if term]
    scored: list[RankedChunk] = []
    for chunk in chunks:
        content = chunk.content.lower()
        score = sum(content.count(term) for term in terms)
        if score > 0:
            scored.append((chunk, float(score)))
    scored.sort(key=lambda pair: (-pair[1], pair[0].chunk_index))
    return scored


async def _semantic_scores(
    session: AsyncSession,
    tenant_id: int,
    query: str,
    embeddings: EmbeddingProvider,
    top_k: int,
) -> list[RankedChunk]:
    """Embed the query and rank embedded chunks by cosine similarity."""
    vectors = await embeddings.embed_texts([query])
    if not vectors:
        raise EmbeddingError("embedding provider returned no vector for the search query")
    query_vector = vectors[0]

    if session.get_bind().dialect.name == "postgresql":
        return await _semantic_scores_pg(session, tenant_id, query_vector, embeddings, top_k)

    chunks = await _fetch_chunks(session, tenant_id)
    scored = [
        (chunk, _cosine_similarity(query_vector, chunk.embedding))
        for chunk in chunks
        if chunk.embedding is not None
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0].chunk_index))
    return scored[:top_k]


async def _semantic_scores_pg(
    session: AsyncSession,
    tenant_id: int,
    query_vector: list[float],
    embeddings: EmbeddingProvider,
    top_k: int,
) -> list[RankedChunk]:
    """PostgreSQL path: pgvector computes cosine distance in the database."""
    from pgvector.sqlalchemy import Vector

    distance = cast(DocumentChunk.embedding, Vector(embeddings.dimension)).cosine_distance(
        query_vector
    )
    rows = await session.execute(
        select(DocumentChunk, distance.label("distance"))
        .where(DocumentChunk.tenant_id == tenant_id, DocumentChunk.embedding.is_not(None))
        .order_by(distance)
        .limit(top_k)
    )
    return [(chunk, 1.0 - float(raw_distance)) for chunk, raw_distance in rows.all()]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors; a zero vector scores 0.0."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _reciprocal_rank_fusion(
    first: list[RankedChunk], second: list[RankedChunk], top_k: int
) -> list[RankedChunk]:
    """Fuse two rankings: ``score = Σ 1/(60 + rank)`` over 1-based ranks."""
    scores: dict[UUID, float] = {}
    chunks: dict[UUID, DocumentChunk] = {}
    for ranking in (first, second):
        for rank, (chunk, _score) in enumerate(ranking, start=1):
            scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (RRF_K + rank)
            chunks[chunk.id] = chunk
    ordered = sorted(scores.items(), key=lambda item: (-item[1], chunks[item[0]].chunk_index))
    return [(chunks[chunk_id], score) for chunk_id, score in ordered[:top_k]]


def _build_hits(ranked: list[RankedChunk], filenames: dict[UUID, str]) -> list[SearchHit]:
    """Project ranked chunks onto the public ``SearchHit`` contract."""
    return [
        SearchHit(
            document_id=chunk.document_id,
            document_filename=filenames.get(chunk.document_id, ""),
            chunk_id=chunk.id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            score=float(score),
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
        )
        for chunk, score in ranked
    ]
