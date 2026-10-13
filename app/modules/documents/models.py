"""Document and chunk models for the ingest pipeline.

Rows are tenant-scoped like every other tenant-owned model (decision A8
simplification): ``Document.tenant_id`` is the isolation key and every
query MUST filter on it. ``DocumentChunk.tenant_id`` is denormalized so
chunk reads in the retrieval layer (T5) never need a join back to
``documents``. Embeddings are added in T5 as a pgvector column on
``DocumentChunk``; the schema here deliberately stays free of them.
"""

import enum
from uuid import UUID, uuid4

from sqlalchemy import Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class DocumentStatus(enum.StrEnum):
    """Lifecycle of a document through the ingest pipeline."""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class Document(TimestampMixin, Base):
    """One uploaded file and its ingest metadata (never the bytes themselves)."""

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.UPLOADED
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class DocumentChunk(TimestampMixin, Base):
    """A text chunk extracted from a document, ready for embedding in T5."""

    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    # Populated by the embedding pipeline in T5; null until then.
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
