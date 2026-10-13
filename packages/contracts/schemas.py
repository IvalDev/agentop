"""Shared request/response schemas; populated as feature tasks land."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Credentials for ``POST /api/v1/auth/login``."""

    email: str
    password: str
    tenant_slug: str


class UserPayload(BaseModel):
    """Public user fields safe to return from auth endpoints."""

    id: int
    email: str
    role: str
    tenant_id: int


class LoginResponse(BaseModel):
    """Issued access token plus the authenticated user's public profile."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserPayload


class MeResponse(BaseModel):
    """Profile returned by ``GET /api/v1/auth/me``."""

    id: int
    email: str
    full_name: str
    role: str
    tenant_id: int


class TokenIntrospectResponse(BaseModel):
    """Validated JWT claims returned by ``POST /api/v1/auth/token/introspect``."""

    sub: str
    tenant_id: int
    role: str
    exp: datetime
    iat: datetime
    iss: str
    aud: str


class AdminProbeResponse(BaseModel):
    """Response of the admin+ role-gate probe endpoint."""

    status: str


class DocumentResponse(BaseModel):
    """Public metadata of a tenant-owned document.

    Mirrors the ``Document`` model minus ``storage_key``/``tenant_id``:
    storage locations and tenant ids are server-internal.
    """

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    created_at: datetime
    error_message: str | None = None


class DocumentListResponse(BaseModel):
    """Caller-scoped listing of documents."""

    documents: list[DocumentResponse]


class SearchRequest(BaseModel):
    """Body of ``POST /api/v1/search``.

    The tenant is never part of the body: it always comes from the
    authenticated user's token claims.
    """

    query: str = Field(min_length=1)
    mode: Literal["semantic", "hybrid", "keyword"] = "hybrid"
    top_k: int = Field(default=10, ge=1, le=50)


class SearchHit(BaseModel):
    """One retrieved chunk with a verifiable citation.

    ``document_id`` + ``start_offset``/``end_offset`` locate the exact
    characters of ``content`` inside the source document (acceptance
    criterion 3).
    """

    document_id: UUID
    document_filename: str
    chunk_id: UUID
    chunk_index: int
    content: str
    score: float
    start_offset: int
    end_offset: int


class SearchResponse(BaseModel):
    """Ranked hits for one search request."""

    query: str
    mode: str
    hits: list[SearchHit]


class ApprovalResponse(BaseModel):
    """Public view of one approval request.

    ``tool_name`` + ``input`` are exactly what will run, and ``action_key``
    is the server-computed binding of both. ``tenant_id`` is server-internal.
    """

    id: UUID
    tool_name: str
    input: dict[str, Any]
    action_key: str
    required_role: str
    status: Literal["pending", "approved", "rejected", "expired", "executing", "executed", "failed"]
    requested_by: int
    created_at: datetime
    expires_at: datetime
    decided_by: int | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    executed_by: int | None = None
    executed_at: datetime | None = None
    execution_result: Any | None = None
    execution_error: str | None = None


class ApprovalListResponse(BaseModel):
    """Caller-tenant listing of approval requests, newest first."""

    approvals: list[ApprovalResponse]


class ApprovalRejectRequest(BaseModel):
    """Optional body of ``POST /api/v1/approvals/{id}/reject``."""

    reason: str | None = Field(default=None, max_length=1000)


class AuditEventResponse(BaseModel):
    """Public view of one audit event (``tenant_id`` is server-internal).

    ``payload``/``result`` are sanitized JSON; an oversized value arrives as
    ``{"truncated": true, "original_bytes": n, "preview": "..."}``.
    """

    id: UUID
    action: str
    actor_type: Literal["user", "agent", "system"]
    actor_user_id: int | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    tool_name: str | None = None
    payload: Any | None = None
    result: Any | None = None
    correlation_id: str | None = None
    created_at: datetime


class AuditEventListResponse(BaseModel):
    """One page of the caller tenant's audit trail, newest first.

    Pass ``next_cursor`` back as ``cursor`` for the next page; ``None`` means
    this is the last page.
    """

    events: list[AuditEventResponse]
    next_cursor: str | None = None
