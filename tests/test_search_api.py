"""Search API tests: authentication, validation, payload, tenant isolation."""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.documents.models import Document, DocumentChunk, DocumentStatus
from app.modules.identity.models import Tenant
from app.modules.retrieval.embeddings import FakeEmbeddingProvider

SEARCH_DIM = 64


@pytest.fixture
def search_settings() -> Settings:
    """Settings with the offline fake provider and a fixed JWT secret."""
    return Settings(
        jwt_secret="test-secret-0123456789-abcdefghijklmnop",
        access_token_expire_minutes=30,
        embedding_provider="fake",
        embedding_dim=SEARCH_DIM,
    )


@pytest.fixture
async def search_client(seeded_db: Database, search_settings: Settings) -> AsyncClient:
    """HTTP client over an app with seeded DB and a fake embedding provider."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = search_settings
    application.state.embeddings = FakeEmbeddingProvider(dimension=SEARCH_DIM)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _headers(
    client: AsyncClient,
    email: str = "owner@acme.test",
    password: str = "owner-pass",
    tenant_slug: str = "acme",
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _seed_document(
    db: Database, tenant_slug: str, filename: str, contents: list[str]
) -> UUID:
    """Insert a processed document with embedded chunks for one tenant."""
    provider = FakeEmbeddingProvider(dimension=SEARCH_DIM)
    async with db.sessionmaker() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one()
        document = Document(
            id=uuid4(),
            tenant_id=tenant.id,
            filename=filename,
            content_type="text/plain",
            size_bytes=1,
            storage_key=f"{tenant.id}/{filename}",
            status=DocumentStatus.PROCESSED,
        )
        session.add(document)
        await session.flush()
        vectors = await provider.embed_texts(contents)
        cursor = 0
        for index, (content, vector) in enumerate(zip(contents, vectors, strict=True)):
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    tenant_id=tenant.id,
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


async def test_search_requires_authentication(search_client: AsyncClient) -> None:
    """POST /search without a token is 401."""
    response = await search_client.post("/api/v1/search", json={"query": "cuentas"})
    assert response.status_code == 401


async def test_search_returns_hits_with_verifiable_citations(
    search_client: AsyncClient, seeded_db: Database
) -> None:
    """Authenticated search returns 200 with citation fields per hit."""
    document_id = await _seed_document(
        seeded_db, "acme", "acme-cuentas.txt", ["resumen de cuentas anual 2025"]
    )
    headers = await _headers(search_client)

    response = await search_client.post(
        "/api/v1/search", headers=headers, json={"query": "cuentas", "mode": "keyword"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == "cuentas"
    assert body["mode"] == "keyword"
    assert body["hits"]
    hit = body["hits"][0]
    assert hit["document_id"] == str(document_id)
    assert hit["document_filename"] == "acme-cuentas.txt"
    assert hit["chunk_index"] == 0
    assert hit["content"] == "resumen de cuentas anual 2025"
    assert isinstance(hit["score"], float)
    assert hit["start_offset"] == 0
    assert hit["end_offset"] == len(hit["content"])
    assert UUID(hit["chunk_id"])


async def test_search_default_mode_is_hybrid(
    search_client: AsyncClient, seeded_db: Database
) -> None:
    """Omitting ``mode`` runs hybrid fusion and echoes the resolved mode."""
    await _seed_document(seeded_db, "acme", "acme.txt", ["factura de electricidad mensual"])
    headers = await _headers(search_client)

    response = await search_client.post(
        "/api/v1/search", headers=headers, json={"query": "factura electricidad"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "hybrid"
    assert body["hits"]
    assert body["hits"][0]["document_filename"] == "acme.txt"


async def test_search_rejects_invalid_mode(search_client: AsyncClient) -> None:
    """An unknown mode is a 422 validation error."""
    headers = await _headers(search_client)
    response = await search_client.post(
        "/api/v1/search", headers=headers, json={"query": "x", "mode": "fuzzy"}
    )
    assert response.status_code == 422


async def test_search_rejects_top_k_zero(search_client: AsyncClient) -> None:
    """``top_k`` below the ge=1 bound is a 422 validation error."""
    headers = await _headers(search_client)
    response = await search_client.post(
        "/api/v1/search", headers=headers, json={"query": "x", "top_k": 0}
    )
    assert response.status_code == 422


async def test_search_rejects_empty_query(search_client: AsyncClient) -> None:
    """An empty query violates min_length=1 and is a 422."""
    headers = await _headers(search_client)
    response = await search_client.post("/api/v1/search", headers=headers, json={"query": ""})
    assert response.status_code == 422


async def test_search_is_tenant_scoped_at_api_level(
    search_client: AsyncClient, seeded_db: Database
) -> None:
    """A globex user never sees acme documents, even matching the query."""
    await _seed_document(seeded_db, "acme", "acme-cuentas.txt", ["resumen de cuentas anual 2025"])
    headers = await _headers(
        search_client, email="owner@globex.test", password="other-pass", tenant_slug="globex"
    )

    response = await search_client.post(
        "/api/v1/search", headers=headers, json={"query": "cuentas", "mode": "keyword"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["hits"] == []
