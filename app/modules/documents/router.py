"""Documents API: upload, listing, fetch, and synchronous ingest.

Tenant identity always comes from the authenticated user's token claims
(``user.tenant_id``); the client never supplies it. Upload and ingest are
operator+ actions; reads are available to any authenticated role.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session
from app.core.deps import get_settings
from app.modules.documents.extract import UnsupportedFormatError
from app.modules.documents.models import Document
from app.modules.documents.service import (
    DocumentNotFoundError,
    FileTooLargeError,
    InvalidFilenameError,
    create_document,
    ingest_document,
)
from app.modules.documents.storage import DocumentStorage
from app.modules.identity.deps import CurrentUser, get_current_user, require_roles
from app.modules.identity.models import UserRole
from app.modules.retrieval.embeddings import EmbeddingError, EmbeddingProvider
from packages.contracts.schemas import DocumentListResponse, DocumentResponse

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
OperatorUserDep = Annotated[
    CurrentUser, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.OPERATOR))
]


def get_storage(request: Request) -> DocumentStorage:
    """Return the storage adapter attached to ``app.state.storage``."""
    return request.app.state.storage


def get_embeddings(request: Request) -> EmbeddingProvider:
    """Return the embedding provider attached to ``app.state.embeddings``."""
    return request.app.state.embeddings


StorageDep = Annotated[DocumentStorage, Depends(get_storage)]
EmbeddingsDep = Annotated[EmbeddingProvider, Depends(get_embeddings)]


def _response(document: Document) -> DocumentResponse:
    """Public document metadata; never exposes ``storage_key``."""
    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        status=document.status.value,
        created_at=document.created_at,
        error_message=document.error_message,
    )


async def _load_document(session: AsyncSession, tenant_id: int, document_id: UUID) -> Document:
    """Fetch a tenant-owned document or raise 404 (also for cross-tenant ids)."""
    result = await session.execute(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document


@router.post("/", response_model=DocumentResponse, status_code=201)
async def upload_document(
    file: UploadFile,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    user: OperatorUserDep,
) -> DocumentResponse:
    """Register an uploaded file for the caller's tenant (operator+)."""
    data = await file.read()
    try:
        document = await create_document(
            session,
            user.tenant_id,
            file.filename or "",
            file.content_type or "",
            data,
            storage,
            settings=settings,
        )
    except InvalidFilenameError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    await session.commit()
    return _response(document)


@router.get("/", response_model=DocumentListResponse)
async def list_documents(session: SessionDep, user: CurrentUserDep) -> DocumentListResponse:
    """Return the caller's documents, newest first."""
    result = await session.execute(
        select(Document)
        .where(Document.tenant_id == user.tenant_id)
        .order_by(Document.created_at.desc())
    )
    return DocumentListResponse(documents=[_response(document) for document in result.scalars()])


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID, session: SessionDep, user: CurrentUserDep
) -> DocumentResponse:
    """Return one tenant-owned document; 404 for missing or foreign ids."""
    document = await _load_document(session, user.tenant_id, document_id)
    return _response(document)


@router.post("/{document_id}/ingest", response_model=DocumentResponse)
async def ingest(
    document_id: UUID,
    session: SessionDep,
    storage: StorageDep,
    embeddings: EmbeddingsDep,
    user: OperatorUserDep,
) -> DocumentResponse:
    """Run extraction, chunking, and embedding synchronously (operator+).

    Extraction failures mark the document ``failed`` (persisted by the
    service) and surface as 422; embedding-backend failures also persist
    ``failed`` and surface as 502 (upstream service unavailable); other
    failures are 500s.
    """
    try:
        document = await ingest_document(
            session, user.tenant_id, document_id, storage, embeddings=embeddings
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await session.commit()
    return _response(document)
