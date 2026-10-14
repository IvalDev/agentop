"""Shared request/response schemas; populated as feature tasks land."""

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


#: Roles a team member can hold, from most to least privileged.
TeamRole = Literal["owner", "admin", "operator", "viewer"]
#: Shortest initial password the team API accepts, in characters.
PASSWORD_MIN_CHARS = 8
#: Longest password in UTF-8 bytes: bcrypt ignores everything past 72 bytes.
PASSWORD_MAX_BYTES = 72
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class TeamUserResponse(BaseModel):
    """Public view of one user of the caller's tenant (no tenant id, never a hash)."""

    id: int
    email: str
    full_name: str
    role: TeamRole
    is_active: bool
    created_at: datetime


class TeamUserListResponse(BaseModel):
    """Every user of the caller's tenant, oldest first."""

    users: list[TeamUserResponse]


class TeamUserCreateRequest(BaseModel):
    """Body of ``POST /api/v1/users``.

    The tenant always comes from the caller's token: a ``tenant_id`` (or any
    other unknown field) is rejected. ``email`` is trimmed and lowercased.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    full_name: str = Field(max_length=200)
    role: TeamRole
    password: str = Field(min_length=PASSWORD_MIN_CHARS)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        email = value.strip().lower()
        if not _EMAIL.fullmatch(email):
            raise ValueError("invalid email address")
        return email

    @field_validator("full_name")
    @classmethod
    def _require_full_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("full name must not be blank")
        return name

    @field_validator("password")
    @classmethod
    def _limit_password_bytes(cls, value: str) -> str:
        if len(value.encode()) > PASSWORD_MAX_BYTES:
            raise ValueError(f"password must be at most {PASSWORD_MAX_BYTES} bytes")
        return value


class TeamUserRoleRequest(BaseModel):
    """Body of ``PUT /api/v1/users/{user_id}/role``."""

    model_config = ConfigDict(extra="forbid")

    role: TeamRole


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


class ModuleResponse(BaseModel):
    """One catalog module with the caller tenant's activation flag.

    The manifest's public identity only: onboarding steps stay server-side
    until the onboarding API (T4) exposes them per user, and ``tenant_id``
    never leaves the server.
    """

    key: str
    name: str
    description: str
    version: str
    permissions: list[str]
    tools: list[str]
    active: bool


class ModuleListResponse(BaseModel):
    """The catalog in registration order with this tenant's activation flags."""

    modules: list[ModuleResponse]


class ModuleActivationResponse(BaseModel):
    """Resulting state of ``POST``/``DELETE`` ``/api/v1/modules/{key}/activate``."""

    key: str
    active: bool


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


#: Lowercase provider identifier inside a module (``caldav``, ``google`` …).
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class ConnectionResponse(BaseModel):
    """Public view of one per-user connection.

    Deliberately carries neither ``tenant_id`` nor any credential: ``metadata``
    is the NON-secret display data only (labels, scopes, endpoint), and the
    Fernet token has no field here at all — a secret cannot leak through
    serialization when it has nowhere to serialize to.
    """

    id: int
    user_id: int
    module_key: str
    provider: str
    status: Literal["pending", "connected", "expired", "revoked", "failed"]
    metadata: dict[str, Any]
    last_health_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ConnectionListResponse(BaseModel):
    """One scope of connections: the caller's own, or the whole tenant (admin)."""

    connections: list[ConnectionResponse]


class ConnectionCreateRequest(BaseModel):
    """Body of ``POST /api/v1/connections`` (create-or-update by natural key).

    Tenant and user always come from the authenticated caller. ``credentials``
    is write-only: encrypted at rest, never echoed, rejected when blank.
    ``metadata`` is non-secret display data; omitted keeps the stored dict.
    """

    model_config = ConfigDict(extra="forbid")

    module_key: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    credentials: str | dict[str, str] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("provider")
    @classmethod
    def _require_lowercase_identifier(cls, value: str) -> str:
        if not _PROVIDER.fullmatch(value):
            raise ValueError("provider must be a lowercase identifier like 'caldav'")
        return value

    @field_validator("credentials")
    @classmethod
    def _reject_blank_credentials(
        cls, value: str | dict[str, str] | None
    ) -> str | dict[str, str] | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            raise ValueError("credentials must not be empty")
        if not isinstance(value, str) and not value:
            raise ValueError("credentials must not be empty")
        return value
