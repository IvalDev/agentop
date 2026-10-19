"""Embedding provider tests: determinism, similarity, and adapter wiring.

The fake provider is the offline backbone of the retrieval test suite;
the Ollama adapter is exercised through ``httpx.MockTransport`` so no
test ever reaches a live server.
"""

import json
import math
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.db import Database
from app.modules.documents.models import Document, DocumentChunk, DocumentStatus
from app.modules.documents.service import create_document, ingest_document
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.models import Tenant
from app.modules.retrieval.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    FakeEmbeddingProvider,
    OllamaEmbeddingProvider,
    build_embedding_provider,
)

MULTI_CHUNK_TEXT = "\n\n".join(f"Section {i}: " + ("factura pago oficina " * 30) for i in range(6))


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


async def test_fake_embedding_is_deterministic() -> None:
    """The same text produces the same vector on every call."""
    provider = FakeEmbeddingProvider(dimension=32)
    first = await provider.embed_texts(["invoice total due"])
    second = await provider.embed_texts(["invoice total due"])
    assert first == second


async def test_fake_embedding_similar_texts_beat_unrelated_texts() -> None:
    """Shared tokens yield a higher cosine similarity than disjoint texts."""
    provider = FakeEmbeddingProvider(dimension=128)
    similar_a, similar_b, unrelated = await provider.embed_texts(
        [
            "the quarterly invoice total is due",
            "the quarterly invoice total is late",
            "garden flowers bloom in spring",
        ]
    )
    assert _cosine(similar_a, similar_b) > 0.5
    assert _cosine(similar_a, similar_b) > _cosine(similar_a, unrelated)


async def test_fake_embedding_respects_dimension_and_normalizes() -> None:
    """Vectors have the configured length and non-empty texts are unit norm."""
    provider = FakeEmbeddingProvider(dimension=24)
    assert provider.dimension == 24
    non_empty, empty = await provider.embed_texts(["hola mundo", ""])
    assert len(non_empty) == 24
    assert math.isclose(math.sqrt(sum(x * x for x in non_empty)), 1.0, rel_tol=1e-9)
    assert empty == [0.0] * 24


async def test_fake_embedding_returns_one_vector_per_text() -> None:
    """Batch input and batch output stay aligned."""
    provider = FakeEmbeddingProvider(dimension=8)
    vectors = await provider.embed_texts(["uno", "dos", "tres", "cuatro"])
    assert len(vectors) == 4


def test_providers_satisfy_the_protocol() -> None:
    """Both adapters are substitutable as ``EmbeddingProvider``."""
    fake = FakeEmbeddingProvider(dimension=8)
    ollama = OllamaEmbeddingProvider(
        base_url="http://localhost:11434", model="nomic-embed-text", dimension=8, timeout=1.0
    )
    assert isinstance(fake, EmbeddingProvider)
    assert isinstance(ollama, EmbeddingProvider)


async def test_ollama_posts_one_prompt_per_text() -> None:
    """Each text becomes an ``/api/embeddings`` call with model + prompt."""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"embedding": [float(len(seen)), 1.0]})

    provider = OllamaEmbeddingProvider(
        base_url="http://ollama.test",
        model="nomic-embed-text",
        dimension=2,
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )
    vectors = await provider.embed_texts(["uno", "dos"])

    assert vectors == [[1.0, 1.0], [2.0, 1.0]]
    assert [path for path, _ in seen] == ["/api/embeddings", "/api/embeddings"]
    assert [body["prompt"] for _, body in seen] == ["uno", "dos"]
    assert all(body["model"] == "nomic-embed-text" for _, body in seen)


async def test_ollama_wraps_http_errors_as_embedding_error() -> None:
    """A non-200 response surfaces as ``EmbeddingError``, not HTTPStatusError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    provider = OllamaEmbeddingProvider(
        base_url="http://ollama.test",
        model="nomic-embed-text",
        dimension=8,
        timeout=1.0,
        transport=transport,
    )
    with pytest.raises(EmbeddingError) as excinfo:
        await provider.embed_texts(["x"])
    assert "500" in str(excinfo.value)


async def test_ollama_wraps_network_failures_as_embedding_error() -> None:
    """Connection failures (closed port) surface as ``EmbeddingError``."""
    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:1", model="m", dimension=8, timeout=1.0
    )
    with pytest.raises(EmbeddingError):
        await provider.embed_texts(["x"])


def test_build_provider_dispatches_on_settings() -> None:
    """``embedding_provider`` selects the adapter and forwards its config."""
    fake = build_embedding_provider(Settings(embedding_provider="fake", embedding_dim=64))
    assert isinstance(fake, FakeEmbeddingProvider)
    assert fake.dimension == 64

    ollama = build_embedding_provider(
        Settings(
            embedding_provider="ollama",
            embedding_dim=32,
            embedding_model="m",
            ollama_base_url="http://ollama.test:11434",
            embedding_timeout_seconds=5.0,
        )
    )
    assert isinstance(ollama, OllamaEmbeddingProvider)
    assert ollama.dimension == 32

    with pytest.raises(ValueError, match="embedding_provider"):
        build_embedding_provider(Settings(embedding_provider="not-a-provider"))


@pytest.fixture
def ingest_settings() -> Settings:
    """Settings for the ingest tests; generous upload cap for chunk texts."""
    return Settings(
        jwt_secret="test-secret-0123456789-abcdefghijklmnop",
        max_upload_bytes=1_000_000,
    )


@pytest.fixture
def ingest_storage(tmp_path) -> LocalDocumentStorage:
    """Local blob storage isolated per test."""
    return LocalDocumentStorage(tmp_path / "storage")


async def _create_acme_document(
    db: Database, storage: LocalDocumentStorage, settings: Settings, content: bytes
) -> tuple[int, UUID]:
    """Upload one plain-text document for the acme tenant; return ids."""
    async with db.sessionmaker() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == "acme"))).scalar_one()
        document = await create_document(
            session,
            tenant.id,
            "notas.txt",
            "text/plain",
            content,
            storage,
            settings=settings,
        )
        await session.commit()
        return tenant.id, document.id


async def test_ingest_persists_offsets_and_embeddings(
    seeded_db: Database, ingest_storage: LocalDocumentStorage, ingest_settings: Settings
) -> None:
    """Chunks carry citation offsets into the source text and their vectors."""
    content = MULTI_CHUNK_TEXT.encode("utf-8")
    tenant_id, document_id = await _create_acme_document(
        seeded_db, ingest_storage, ingest_settings, content
    )
    provider = FakeEmbeddingProvider(dimension=48)

    async with seeded_db.sessionmaker() as session:
        document = await ingest_document(
            session, tenant_id, document_id, ingest_storage, embeddings=provider
        )
        await session.commit()
    assert document.status == DocumentStatus.PROCESSED

    async with seeded_db.sessionmaker() as session:
        rows = list(
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars()
        )

    assert len(rows) > 1
    for row in rows:
        assert MULTI_CHUNK_TEXT[row.start_offset : row.end_offset] == row.content
        assert row.embedding is not None
        assert len(row.embedding) == 48
    assert rows[0].start_offset == 0
    assert rows[-1].end_offset == len(MULTI_CHUNK_TEXT)


async def test_ingest_without_provider_leaves_embeddings_null(
    seeded_db: Database, ingest_storage: LocalDocumentStorage, ingest_settings: Settings
) -> None:
    """Backwards compatibility: no provider passed means null embeddings."""
    tenant_id, document_id = await _create_acme_document(
        seeded_db, ingest_storage, ingest_settings, b"contenido breve de prueba"
    )

    async with seeded_db.sessionmaker() as session:
        await ingest_document(session, tenant_id, document_id, ingest_storage)
        await session.commit()

    async with seeded_db.sessionmaker() as session:
        embeddings = list(
            (
                await session.execute(
                    select(DocumentChunk.embedding).where(DocumentChunk.document_id == document_id)
                )
            ).scalars()
        )
    assert embeddings
    assert all(vector is None for vector in embeddings)


class _FailingEmbeddingProvider:
    """Stub provider that always fails, for the failure-commit contract."""

    dimension = 8

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("embedding backend unavailable")


async def test_embedding_failure_marks_document_failed_without_orphan_chunks(
    seeded_db: Database, ingest_storage: LocalDocumentStorage, ingest_settings: Settings
) -> None:
    """A failed embed commits ``failed`` and leaves no chunks behind."""
    tenant_id, document_id = await _create_acme_document(
        seeded_db, ingest_storage, ingest_settings, b"texto que no llegara a indexarse"
    )

    with pytest.raises(EmbeddingError, match="unavailable"):
        async with seeded_db.sessionmaker() as session:
            await ingest_document(
                session,
                tenant_id,
                document_id,
                ingest_storage,
                embeddings=_FailingEmbeddingProvider(),
            )

    async with seeded_db.sessionmaker() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        assert document.status == DocumentStatus.FAILED
        assert document.error_message is not None
        assert "unavailable" in document.error_message
        chunk_count = (
            await session.execute(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
            )
        ).scalar_one()
    assert chunk_count == 0
