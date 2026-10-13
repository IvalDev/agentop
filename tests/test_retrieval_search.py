"""Search service tests: semantic, keyword, and hybrid ranking per tenant.

Everything runs against the SQLite fixture database, so semantic ranking
exercises the Python-side cosine path. Vectors come from
:class:`FakeEmbeddingProvider`, which makes rankings deterministic.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.db import Database
from app.modules.documents.models import Document, DocumentChunk, DocumentStatus
from app.modules.identity.models import Tenant
from app.modules.retrieval.embeddings import FakeEmbeddingProvider
from app.modules.retrieval.service import search

DIM = 64


@pytest.fixture
def provider() -> FakeEmbeddingProvider:
    """Shared fake embedder for seeding and querying."""
    return FakeEmbeddingProvider(dimension=DIM)


async def _tenant_id(db: Database, slug: str) -> int:
    async with db.sessionmaker() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        return tenant.id


async def _seed_document(
    db: Database,
    tenant_id: int,
    filename: str,
    contents: list[str],
    provider: FakeEmbeddingProvider,
    *,
    embed: bool = True,
) -> UUID:
    """Insert a processed document with one chunk (and vector) per content."""
    async with db.sessionmaker() as session:
        document = Document(
            id=uuid4(),
            tenant_id=tenant_id,
            filename=filename,
            content_type="text/plain",
            size_bytes=1,
            storage_key=f"{tenant_id}/{filename}",
            status=DocumentStatus.PROCESSED,
        )
        session.add(document)
        await session.flush()
        vectors = await provider.embed_texts(contents) if embed else [None] * len(contents)
        cursor = 0
        for index, (content, vector) in enumerate(zip(contents, vectors, strict=True)):
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    tenant_id=tenant_id,
                    chunk_index=index,
                    content=content,
                    start_offset=cursor,
                    end_offset=cursor + len(content),
                    embedding=vector,
                )
            )
            cursor += len(content)
        await session.commit()
        return document.id


async def test_semantic_search_ranks_shared_token_chunk_first(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """The chunk sharing the query tokens ranks first, with citation offsets."""
    acme = await _tenant_id(seeded_db, "acme")
    acme_doc = await _seed_document(
        seeded_db,
        acme,
        "acme.txt",
        [
            "invoice payment overdue notice for accounting",
            "office plants need water weekly",
            "team meeting agenda for monday",
        ],
        provider,
    )

    async with seeded_db.sessionmaker() as session:
        hits = await search(session, acme, "invoice payment overdue", "semantic", 10, provider)

    assert hits
    assert hits[0].content.startswith("invoice payment overdue notice")
    assert hits[0].document_id == acme_doc
    assert hits[0].document_filename == "acme.txt"
    assert hits[0].chunk_index == 0
    assert hits[0].start_offset == 0
    assert hits[0].end_offset == len(hits[0].content)
    assert hits[0].score >= hits[1].score


async def test_semantic_search_never_returns_other_tenants(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """A query matching another tenant's chunk yields no foreign hits."""
    acme = await _tenant_id(seeded_db, "acme")
    globex = await _tenant_id(seeded_db, "globex")
    await _seed_document(
        seeded_db, acme, "acme.txt", ["unrelated acme note about plants"], provider
    )
    globex_doc = await _seed_document(
        seeded_db, globex, "globex.txt", ["invoice payment overdue from globex billing"], provider
    )

    async with seeded_db.sessionmaker() as session:
        hits = await search(session, acme, "invoice payment overdue", "semantic", 10, provider)

    assert all(hit.document_id != globex_doc for hit in hits)


async def test_semantic_search_skips_chunks_without_embeddings(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """Only chunks with a non-null embedding participate in semantic ranking."""
    acme = await _tenant_id(seeded_db, "acme")
    await _seed_document(
        seeded_db,
        acme,
        "pending.txt",
        ["invoice payment overdue notice for accounting"],
        provider,
        embed=False,
    )
    embedded_doc = await _seed_document(
        seeded_db, acme, "ready.txt", ["totally different garden content"], provider
    )

    async with seeded_db.sessionmaker() as session:
        hits = await search(session, acme, "invoice payment overdue", "semantic", 10, provider)

    assert hits
    assert all(hit.document_id == embedded_doc for hit in hits)
    assert all("invoice payment overdue" not in hit.content for hit in hits)


async def test_keyword_search_ranks_by_case_insensitive_occurrence_count(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """More occurrences of the query term rank higher; case is ignored."""
    acme = await _tenant_id(seeded_db, "acme")
    double, single, other = (
        "cuentas de clientes y cuentas de proveedores",
        "resumen de cuentas anual",
        "notas de reunion diversas",
    )
    await _seed_document(seeded_db, acme, "acme.txt", [single, double, other], provider)

    async with seeded_db.sessionmaker() as session:
        hits = await search(session, acme, "CUENTAS", "keyword", 10, provider)

    assert [hit.content for hit in hits] == [double, single]
    assert hits[0].score == 2.0
    assert hits[1].score == 1.0
    assert hits[0].chunk_index == 1


async def test_keyword_search_returns_nothing_without_matches(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """A query whose terms appear nowhere scores nothing (no filler hits)."""
    acme = await _tenant_id(seeded_db, "acme")
    await _seed_document(seeded_db, acme, "acme.txt", ["notas de reunion diversas"], provider)

    async with seeded_db.sessionmaker() as session:
        hits = await search(session, acme, "facturacion electronica", "keyword", 10, provider)

    assert hits == []


async def test_keyword_search_is_tenant_scoped(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """A term present only in another tenant's chunk never surfaces."""
    acme = await _tenant_id(seeded_db, "acme")
    globex = await _tenant_id(seeded_db, "globex")
    await _seed_document(seeded_db, acme, "acme.txt", ["notas de reunion diversas"], provider)
    await _seed_document(seeded_db, globex, "globex.txt", ["cuentas de clientes globex"], provider)

    async with seeded_db.sessionmaker() as session:
        hits = await search(session, acme, "cuentas", "keyword", 10, provider)

    assert hits == []


async def test_hybrid_fuses_keyword_and_semantic_rankings(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """A chunk strong in only one list still appears in fused results."""
    acme = await _tenant_id(seeded_db, "acme")
    keyword_strong = "alpha alpha alpha " + "filler token " * 150
    semantic_strong = "alpha"
    distractor = "alpha meeting room details"
    await _seed_document(
        seeded_db, acme, "acme.txt", [keyword_strong, semantic_strong, distractor], provider
    )

    async with seeded_db.sessionmaker() as session:
        keyword_hits = await search(session, acme, "alpha", "keyword", 10, provider)
        semantic_hits = await search(session, acme, "alpha", "semantic", 10, provider)
        hybrid_hits = await search(session, acme, "alpha", "hybrid", 2, provider)

    # Premise: each chunk dominates a different list.
    assert keyword_hits[0].content == keyword_strong
    assert semantic_hits[0].content == semantic_strong
    assert semantic_hits[-1].content == keyword_strong

    # Fusion keeps both specialists in a tight top-2.
    hybrid_contents = {hit.content for hit in hybrid_hits}
    assert keyword_strong in hybrid_contents
    assert semantic_strong in hybrid_contents
    assert hybrid_hits[0].score >= hybrid_hits[1].score


async def test_search_rejects_unknown_mode(
    seeded_db: Database, provider: FakeEmbeddingProvider
) -> None:
    """Anything outside the three modes is a programming error, not a 500."""
    acme = await _tenant_id(seeded_db, "acme")
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ValueError, match="mode"):
            await search(session, acme, "alpha", "fuzzy", 10, provider)
