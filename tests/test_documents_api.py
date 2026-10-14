"""Documents API tests: upload validation, tenant isolation, and ingest."""

from io import BytesIO
from uuid import UUID

import pytest
from docx import Document as DocxDocument
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.documents.models import DocumentChunk, DocumentStatus
from app.modules.documents.service import create_document
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.models import Tenant

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def documents_settings() -> Settings:
    """Settings with a small upload cap so the size-limit test stays fast."""
    return Settings(
        jwt_secret="test-secret-0123456789-abcdefghijklmnop",
        access_token_expire_minutes=30,
        max_upload_bytes=1_000_000,
    )


@pytest.fixture
async def documents_client(
    seeded_db: Database, documents_settings: Settings, tmp_path
) -> AsyncClient:
    """HTTP client bound to an app with seeded DB and local storage in tmp."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = documents_settings
    application.state.storage = LocalDocumentStorage(tmp_path / "storage")
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _login(client: AsyncClient, email: str, password: str, tenant_slug: str = "acme") -> dict:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _headers(
    client: AsyncClient,
    email: str = "owner@acme.test",
    password: str = "owner-pass",
    tenant_slug: str = "acme",
) -> dict[str, str]:
    body = await _login(client, email, password, tenant_slug)
    return _bearer(body["access_token"])


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    filename: str = "notes.txt",
    content: bytes = b"plain content",
    content_type: str = "text/plain",
):
    return await client.post(
        "/api/v1/documents/",
        headers=headers,
        files={"file": (filename, content, content_type)},
    )


async def _chunk_meta(db: Database, document_id: str) -> list[tuple[int, str]]:
    """Return (chunk_index, content) rows for a document, ordered by index."""
    async with db.sessionmaker() as session:
        result = await session.execute(
            select(DocumentChunk.chunk_index, DocumentChunk.content)
            .where(DocumentChunk.document_id == UUID(document_id))
            .order_by(DocumentChunk.chunk_index)
        )
        return [(index, content) for index, content in result.all()]


async def test_upload_requires_authentication(documents_client: AsyncClient) -> None:
    """POST /documents/ without a token is 401."""
    response = await _upload(documents_client, headers={})
    assert response.status_code == 401


async def test_list_requires_authentication(documents_client: AsyncClient) -> None:
    """GET /documents/ without a token is 401."""
    response = await documents_client.get("/api/v1/documents/")
    assert response.status_code == 401


async def test_upload_returns_201_uploaded_metadata(documents_client: AsyncClient) -> None:
    """Owner upload succeeds with public metadata only, status uploaded."""
    headers = await _headers(documents_client)
    payload = b"hola mundo"
    response = await _upload(documents_client, headers, content=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    UUID(body["id"])  # must be a UUID string
    assert body["filename"] == "notes.txt"
    assert body["content_type"] == "text/plain"
    assert body["size_bytes"] == len(payload)
    assert body["status"] == DocumentStatus.UPLOADED.value
    assert body["created_at"]
    assert "storage_key" not in body


async def test_upload_rejects_traversal_filename(documents_client: AsyncClient) -> None:
    """Path-traversal filenames are rejected with 422 and not persisted."""
    headers = await _headers(documents_client)
    response = await _upload(documents_client, headers, filename="../../etc/passwd")
    assert response.status_code == 422
    listing = await documents_client.get("/api/v1/documents/", headers=headers)
    assert listing.json()["documents"] == []


async def test_upload_forbids_viewer_role(documents_client: AsyncClient) -> None:
    """Viewer role cannot upload (operator+ endpoint)."""
    headers = await _headers(documents_client, email="viewer@acme.test", password="viewer-pass")
    response = await _upload(documents_client, headers)
    assert response.status_code == 403


async def test_upload_rejects_oversized_file(documents_client: AsyncClient) -> None:
    """Bytes above max_upload_bytes are rejected with 413."""
    headers = await _headers(documents_client)
    response = await _upload(documents_client, headers, content=b"x" * 1_000_001)
    assert response.status_code == 413


async def test_list_is_tenant_scoped(documents_client: AsyncClient) -> None:
    """Each tenant only lists its own documents, newest first."""
    acme_headers = await _headers(documents_client)
    globex_headers = await _headers(
        documents_client, email="owner@globex.test", password="other-pass", tenant_slug="globex"
    )
    acme_upload = await _upload(documents_client, acme_headers, filename="acme.txt")
    globex_upload = await _upload(documents_client, globex_headers, filename="globex.txt")
    assert acme_upload.status_code == 201
    assert globex_upload.status_code == 201

    acme_list = await documents_client.get("/api/v1/documents/", headers=acme_headers)
    globex_list = await documents_client.get("/api/v1/documents/", headers=globex_headers)
    acme_names = [doc["filename"] for doc in acme_list.json()["documents"]]
    globex_names = [doc["filename"] for doc in globex_list.json()["documents"]]
    assert acme_names == ["acme.txt"]
    assert globex_names == ["globex.txt"]


async def test_get_returns_own_document(documents_client: AsyncClient) -> None:
    """GET by id returns the caller's document."""
    headers = await _headers(documents_client)
    upload = await _upload(documents_client, headers)
    document_id = upload.json()["id"]
    response = await documents_client.get(f"/api/v1/documents/{document_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["id"] == document_id


async def test_get_cross_tenant_is_404(documents_client: AsyncClient) -> None:
    """A document from another tenant is indistinguishable from a missing one."""
    acme_headers = await _headers(documents_client)
    globex_headers = await _headers(
        documents_client, email="owner@globex.test", password="other-pass", tenant_slug="globex"
    )
    upload = await _upload(documents_client, acme_headers)
    document_id = upload.json()["id"]
    response = await documents_client.get(
        f"/api/v1/documents/{document_id}", headers=globex_headers
    )
    assert response.status_code == 404


async def test_get_rejects_malformed_uuid(documents_client: AsyncClient) -> None:
    """A non-UUID path segment is a 422, not a 500."""
    headers = await _headers(documents_client)
    response = await documents_client.get("/api/v1/documents/not-a-uuid", headers=headers)
    assert response.status_code == 422


async def test_ingest_marks_processed_and_creates_chunks(
    documents_client: AsyncClient, seeded_db: Database
) -> None:
    """Ingest extracts text, stores chunks, and flips status to processed."""
    headers = await _headers(documents_client)
    text = "Primera línea.\n\nSegundo párrafo con más detalle."
    upload = await _upload(documents_client, headers, content=text.encode())
    document_id = upload.json()["id"]

    response = await documents_client.post(
        f"/api/v1/documents/{document_id}/ingest", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == DocumentStatus.PROCESSED.value

    chunks = await _chunk_meta(seeded_db, document_id)
    assert [index for index, _ in chunks] == list(range(len(chunks)))
    assert "".join(content for _, content in chunks) == text


async def test_ingest_chunks_long_text_sequentially(
    documents_client: AsyncClient, seeded_db: Database
) -> None:
    """Long text yields multiple chunks with 0-based sequential indexes."""
    headers = await _headers(documents_client)
    text = "\n\n".join(f"Section {i} " + "word " * 80 for i in range(8))
    upload = await _upload(documents_client, headers, content=text.encode())
    document_id = upload.json()["id"]

    response = await documents_client.post(
        f"/api/v1/documents/{document_id}/ingest", headers=headers
    )
    assert response.status_code == 200, response.text

    chunks = await _chunk_meta(seeded_db, document_id)
    assert len(chunks) > 1
    assert [index for index, _ in chunks] == list(range(len(chunks)))


async def test_ingest_unsupported_format_marks_failed(
    documents_client: AsyncClient, seeded_db: Database
) -> None:
    """Unsupported content types fail ingest with 422 and persist status failed."""
    headers = await _headers(documents_client)
    upload = await _upload(
        documents_client,
        headers,
        filename="bundle.zip",
        content=b"PK\x03\x04",
        content_type="application/zip",
    )
    document_id = upload.json()["id"]

    response = await documents_client.post(
        f"/api/v1/documents/{document_id}/ingest", headers=headers
    )
    assert response.status_code == 422
    assert "unsupported" in response.json()["detail"]

    fetched = await documents_client.get(f"/api/v1/documents/{document_id}", headers=headers)
    body = fetched.json()
    assert body["status"] == DocumentStatus.FAILED.value
    assert "unsupported" in body["error_message"]


async def test_ingest_docx_extracts_text(
    documents_client: AsyncClient, seeded_db: Database
) -> None:
    """A DOCX upload is extracted through the python-docx path end to end."""
    document = DocxDocument()
    document.add_paragraph("Reporte trimestral")
    buffer = BytesIO()
    document.save(buffer)

    headers = await _headers(documents_client)
    upload = await _upload(
        documents_client,
        headers,
        filename="reporte.docx",
        content=buffer.getvalue(),
        content_type=DOCX_CONTENT_TYPE,
    )
    document_id = upload.json()["id"]

    response = await documents_client.post(
        f"/api/v1/documents/{document_id}/ingest", headers=headers
    )
    assert response.status_code == 200, response.text
    chunks = await _chunk_meta(seeded_db, document_id)
    assert any("Reporte trimestral" in content for _, content in chunks)


async def test_generic_markdown_upload_is_stored_as_markdown_and_ingests(
    documents_client: AsyncClient,
) -> None:
    """``curl -F file=@x.md`` sends octet-stream; the type comes from the extension instead."""
    headers = await _headers(documents_client)
    upload = await _upload(
        documents_client,
        headers,
        filename="policy.md",
        content=b"# Leave policy\n\nRequest leave 15 days in advance.",
        content_type="application/octet-stream",
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["content_type"] == "text/markdown"

    response = await documents_client.post(
        f"/api/v1/documents/{upload.json()['id']}/ingest", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == DocumentStatus.PROCESSED.value
    assert response.json()["content_type"] == "text/markdown"


async def test_upload_keeps_explicit_client_type(documents_client: AsyncClient) -> None:
    """A specific type sent by the client wins over the filename extension."""
    headers = await _headers(documents_client)
    upload = await _upload(
        documents_client, headers, filename="notes.md", content_type="text/plain"
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["content_type"] == "text/plain"


@pytest.mark.parametrize(
    ("filename", "sent_type", "stored_type"),
    [
        ("notes.md", "", "text/markdown"),
        ("NOTES.MARKDOWN", "application/octet-stream", "text/markdown"),
        ("notes.txt", "", "text/plain"),
        ("scan.pdf", "application/octet-stream", "application/pdf"),
        ("report.docx", "", DOCX_CONTENT_TYPE),
        ("bundle.zip", "application/octet-stream", "application/octet-stream"),
        ("README", "", ""),
        ("notes.md", "text/plain", "text/plain"),
    ],
)
async def test_create_document_infers_generic_type_from_extension(
    seeded_db: Database,
    documents_settings: Settings,
    tmp_path,
    filename: str,
    sent_type: str,
    stored_type: str,
) -> None:
    """Only an empty or octet-stream type is replaced, and only for extensions ingest reads."""
    async with seeded_db.sessionmaker() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == "acme"))).scalar_one()
        document = await create_document(
            session,
            tenant.id,
            filename,
            sent_type,
            b"content",
            LocalDocumentStorage(tmp_path),
            settings=documents_settings,
        )
    assert document.content_type == stored_type


async def test_ingest_forbids_viewer_role(documents_client: AsyncClient) -> None:
    """Viewer role cannot trigger ingest (operator+ endpoint)."""
    owner_headers = await _headers(documents_client)
    upload = await _upload(documents_client, owner_headers)
    document_id = upload.json()["id"]
    viewer_headers = await _headers(
        documents_client, email="viewer@acme.test", password="viewer-pass"
    )
    response = await documents_client.post(
        f"/api/v1/documents/{document_id}/ingest", headers=viewer_headers
    )
    assert response.status_code == 403


async def test_ingest_cross_tenant_is_404(documents_client: AsyncClient) -> None:
    """Ingest from another tenant cannot see or touch the document."""
    acme_headers = await _headers(documents_client)
    globex_headers = await _headers(
        documents_client, email="owner@globex.test", password="other-pass", tenant_slug="globex"
    )
    upload = await _upload(documents_client, acme_headers)
    document_id = upload.json()["id"]
    response = await documents_client.post(
        f"/api/v1/documents/{document_id}/ingest", headers=globex_headers
    )
    assert response.status_code == 404


async def test_upload_ingest_and_rejections_are_audited(
    documents_client: AsyncClient, seeded_db: Database, audit_log
) -> None:
    """Every upload/ingest outcome over HTTP leaves an audit event for the caller."""
    headers = await _headers(documents_client)
    me = (await documents_client.get("/api/v1/auth/me", headers=headers)).json()
    ok = (await _upload(documents_client, headers)).json()["id"]
    zipped = (
        await _upload(
            documents_client,
            headers,
            filename="b.zip",
            content=b"PK",
            content_type="application/zip",
        )
    ).json()["id"]
    await _upload(documents_client, headers, filename="../escape.txt")
    for document_id in (ok, zipped):
        await documents_client.post(f"/api/v1/documents/{document_id}/ingest", headers=headers)

    logged = await audit_log(seeded_db)
    assert {(event.actor_type, event.actor_user_id) for event in logged} == {("user", me["id"])}
    events = {(event.action, event.resource_id) for event in logged}
    assert events == {
        ("document.uploaded", ok),
        ("document.uploaded", zipped),
        ("document.upload_rejected", None),
        ("document.ingested", ok),
        ("document.ingest_failed", zipped),
    }
