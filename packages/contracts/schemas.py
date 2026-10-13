"""Shared request/response schemas; populated as feature tasks land."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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


#: Longest chat message (and history turn) the API accepts, in characters.
CHAT_MESSAGE_MAX_CHARS = 4000
#: Most prior turns a client may replay as ``history``.
CHAT_HISTORY_MAX_MESSAGES = 20


class ChatHistoryMessage(BaseModel):
    """One prior turn replayed by the client.

    Only ``user`` and ``assistant`` turns exist here: a client can never add
    a ``system`` or ``tool`` message to the conversation.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=CHAT_MESSAGE_MAX_CHARS)


class ChatRequest(BaseModel):
    """Body of ``POST /api/v1/chat`` and ``/api/v1/chat/stream``.

    The tenant always comes from the authenticated user, never the body.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=CHAT_MESSAGE_MAX_CHARS)
    history: list[ChatHistoryMessage] = Field(
        default_factory=list, max_length=CHAT_HISTORY_MAX_MESSAGES
    )


class ChatCitation(BaseModel):
    """A numbered passage the agent read, verifiable against its document.

    ``number`` is the ``[n]`` marker used in the answer; the stored document
    text satisfies ``text[start_offset:end_offset] == content``.
    """

    number: int
    document_id: UUID
    document_filename: str
    chunk_id: UUID
    chunk_index: int
    start_offset: int
    end_offset: int
    content: str


class PendingApproval(BaseModel):
    """An action the agent submitted for human approval (not executed)."""

    approval_id: UUID
    tool_name: str
    expires_at: datetime


class ToolCallSummary(BaseModel):
    """Outcome of one model tool call (error codes only, never handler details)."""

    call_id: str
    tool_name: str
    status: Literal["ok", "error", "approval_required"]
    error: str | None = None
    approval_id: UUID | None = None


class ChatResponse(BaseModel):
    """Result of one agent run.

    ``citations`` are the passages whose ``[n]`` markers appear in
    ``answer``; ``sources`` are every passage the model was shown.
    ``stop_reason`` is ``step_limit`` when the run hit the step budget.
    """

    run_id: str
    answer: str
    stop_reason: Literal["completed", "step_limit"]
    citations: list[ChatCitation]
    sources: list[ChatCitation]
    pending_approvals: list[PendingApproval]
    tool_calls: list[ToolCallSummary]
