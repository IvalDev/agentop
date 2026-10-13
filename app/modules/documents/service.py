"""Document service: validated upload creation and tenant-scoped ingest.

Every query filters on ``tenant_id`` (mirrors identity's
:func:`scoped_query`): a document id from another tenant must behave
exactly like a missing one.
"""

import logging
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.documents.chunking import chunk_text
from app.modules.documents.extract import extract_text
from app.modules.documents.models import Document, DocumentChunk, DocumentStatus
from app.modules.documents.storage import DocumentStorage
from app.modules.retrieval.embeddings import EmbeddingError, EmbeddingProvider

logger = logging.getLogger(__name__)

MAX_FILENAME_LENGTH = 255


class DocumentNotFoundError(Exception):
    """Raised when a document id is unknown within the caller's tenant."""


class InvalidFilenameError(Exception):
    """Raised when an upload filename fails validation."""


class FileTooLargeError(Exception):
    """Raised when the upload exceeds ``settings.max_upload_bytes``."""


def validate_filename(filename: str) -> str:
    """Return the stripped filename when safe to store, else raise.

    Rejects empty names, names above :data:`MAX_FILENAME_LENGTH`, and any
    path syntax (``..``, ``/``, ``\\``) — storage keys embed the filename,
    so traversal here would become traversal on disk.
    """
    name = filename.strip()
    if not name:
        raise InvalidFilenameError("filename must not be empty")
    if len(name) > MAX_FILENAME_LENGTH:
        raise InvalidFilenameError(f"filename must be at most {MAX_FILENAME_LENGTH} characters")
    if ".." in name or "/" in name or "\\" in name:
        raise InvalidFilenameError("filename must not contain '..', '/', or '\\'")
    return name


def storage_key_for(tenant_id: int, document_id: UUID, filename: str) -> str:
    """Build the storage key for a document (A4: tenant prefix first)."""
    return f"{tenant_id}/{document_id}/{filename}"


async def create_document(
    session: AsyncSession,
    tenant_id: int,
    filename: str,
    content_type: str,
    data: bytes,
    storage: DocumentStorage,
    *,
    settings: Settings,
) -> Document:
    """Validate, store, and register a new document in ``uploaded`` state.

    The caller owns the transaction and must commit. Bytes reach storage
    before the row exists: a failed commit can orphan a blob, which the
    T12 lifecycle/backup story cleans up; the inverse (row without bytes)
    would break ingest instead.
    """
    name = validate_filename(filename)
    if len(data) > settings.max_upload_bytes:
        raise FileTooLargeError(
            f"file is {len(data)} bytes; limit is {settings.max_upload_bytes} bytes"
        )
    document_id = uuid4()
    key = storage_key_for(tenant_id, document_id, name)
    await storage.put(key, data)
    document = Document(
        id=document_id,
        tenant_id=tenant_id,
        filename=name,
        content_type=content_type,
        size_bytes=len(data),
        storage_key=key,
        status=DocumentStatus.UPLOADED,
    )
    session.add(document)
    return document


async def ingest_document(
    session: AsyncSession,
    tenant_id: int,
    document_id: UUID,
    storage: DocumentStorage,
    *,
    embeddings: EmbeddingProvider | None = None,
) -> Document:
    """Extract, chunk, embed, and index one tenant-owned document.

    On success the caller commits the ``processed`` status and the new
    chunks (the worker's ``session_scope`` or the router). On extraction
    failure this function commits the ``failed`` status itself before
    re-raising, so the failure survives the caller's rollback and the
    client can read ``error_message`` afterwards.

    When ``embeddings`` is provided, every chunk is embedded in a single
    ``embed_texts`` call and the vectors stored on the chunk rows. An
    embedding failure follows the same commit pattern — the pending chunk
    insert is rolled back (no orphaned rows or vectors), the ``failed``
    status with a clear ``error_message`` is committed, and the error is
    re-raised as :class:`EmbeddingError`. Without a provider the chunks
    simply keep null embeddings.
    """
    result = await session.execute(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise DocumentNotFoundError(f"document {document_id} not found")

    document.status = DocumentStatus.PROCESSING
    document.error_message = None
    await session.flush()

    try:
        data = await storage.get(document.storage_key)
        text = extract_text(data, document.content_type)
        chunks = chunk_text(text)
    except Exception as exc:
        document.status = DocumentStatus.FAILED
        document.error_message = str(exc) or type(exc).__name__
        await session.commit()
        logger.warning(
            "document ingest failed",
            extra={"document_id": str(document_id), "tenant_id": tenant_id},
        )
        raise

    # Re-ingest must not duplicate chunks: replace any previous extraction.
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    chunk_rows = [
        DocumentChunk(
            document_id=document.id,
            tenant_id=tenant_id,
            chunk_index=index,
            content=chunk,
            start_offset=start_offset,
            end_offset=end_offset,
        )
        for index, (chunk, start_offset, end_offset) in enumerate(chunks)
    ]
    session.add_all(chunk_rows)

    if embeddings is not None and chunk_rows:
        try:
            vectors = await embeddings.embed_texts([row.content for row in chunk_rows])
            if len(vectors) != len(chunk_rows):
                raise EmbeddingError(
                    f"embedding provider returned {len(vectors)} vectors "
                    f"for {len(chunk_rows)} chunks"
                )
        except Exception as exc:
            await _commit_embedding_failure(session, tenant_id, document_id, exc)
            if isinstance(exc, EmbeddingError):
                raise
            raise EmbeddingError(str(exc) or type(exc).__name__) from exc
        for row, vector in zip(chunk_rows, vectors, strict=True):
            row.embedding = vector

    document.status = DocumentStatus.PROCESSED
    return document


async def _commit_embedding_failure(
    session: AsyncSession, tenant_id: int, document_id: UUID, exc: Exception
) -> None:
    """Discard pending chunks and persist the ``failed`` embedding status.

    Mirrors the extraction-failure commit above: the rollback drops the
    uncommitted chunk rows (and their vectors), then a fresh load of the
    document commits ``failed`` so the error survives the caller's own
    rollback.
    """
    await session.rollback()
    result = await session.execute(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    document = result.scalar_one()
    document.status = DocumentStatus.FAILED
    document.error_message = str(exc) or type(exc).__name__
    await session.commit()
    logger.warning(
        "document embedding failed",
        extra={"document_id": str(document_id), "tenant_id": tenant_id},
    )
