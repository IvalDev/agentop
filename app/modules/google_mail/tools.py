"""Google Mail chat tools (G3, google-mail M3): scoped reads and gated actions.

Six ToolSpecs on module ``google_mail``, registered through
:func:`register_google_mail_tools`: the production fold in
:func:`~app.modules.google_mail.registry.register_default_google_mail`
registers them once at import time on the process-wide
:data:`~app.modules.tools.registry.default_registry` (the reviewed
google-mail G3 allowlist entry, locked by ``tests/test_tools_executor.py``);
tests call it against a LOCAL
:class:`~app.modules.tools.registry.ToolRegistry`.

Tool shapes (M3 — the approval policies differ on purpose, criterion 4):

- ``search_emails`` — ``EXTRACT`` / viewer, ``ALLOW_WITHOUT_APPROVAL``:
  bounded message search (optional free-text ``query``, 1–50 results);
  ALWAYS filtered by the scope's ``labelIds`` unless the user chose the
  whole mailbox (``ALL``) — Google never filters per label, our backend
  does (risk #2);
- ``get_email`` — ``EXTRACT`` / viewer, no approval: one full message,
  refused with the actionable ``OUT_OF_SCOPE`` reason when the message's
  ``labelIds`` do not contain the selected label (criterion 3's heart);
  ``ALL`` skips the check;
- ``list_labels`` — ``EXTRACT`` / viewer, no approval: the mailbox's
  label ids + display names (metadata only);
- ``send_email`` — ``ACTION`` / operator, ``REQUIRE_APPROVAL``: builds a
  proper RFC 822 MIME (stdlib ``email``), base64url-encodes it, and sends
  it (``threadId`` when replying). EXEMPT from the label-membership check
  — a new message belongs to no folder — but approval is ALWAYS required;
- ``archive_message`` — ``ACTION`` / operator, ``REQUIRE_APPROVAL``:
  ``modify`` removing ``INBOX``, only after verifying the message is
  inside the scope;
- ``add_label(message_id, label_id)`` — ``ACTION`` / operator,
  ``REQUIRE_APPROVAL``: ``modify`` adding a label, only after verifying
  the TARGET MESSAGE is inside the scope (the label being applied may be
  any label except ``TRASH``/``SPAM`` — the constraint is on the message
  acted upon).

No delete/trash tool, ever (MVP decision): ``add_label`` refuses ``TRASH``
and ``SPAM`` at validation, since Gmail purges both after 30 days.

Approval enforcement (decision A7, criterion 4): the engine's real
mechanism is ``ToolSpec.approval_policy = ApprovalPolicy.REQUIRE_APPROVAL``.
The executor stops a ``REQUIRE_APPROVAL`` call with no
``approval_grant`` at ``approval_required`` — the handler (and therefore
Gmail) is never touched — and only the approvals service mints the
single-use grant that later unlocks exactly that tool + input
(``action_key``), re-running the precheck right before the handler.
``tests/test_google_mail_tools.py`` proves both halves per action tool.

Shared precheck (never a 500, never leaks the blob or tokens): the G2
accessor :func:`~app.modules.google_mail.scope.get_effective_scope` must
answer a persisted selection backed by a stored connection (else
``NOT_CONFIGURED``), and the access token must be usable — an expired one
is refreshed on demand and persisted (the calendar/G2 mechanism); a refused
or impossible refresh answers the ``EXPIRED`` "reconnect" reason. Every
failure is a :class:`~app.modules.tools.exceptions.ToolPrecheckError`
with a short, English, actionable reason the model can relay to the user.

The message-targeting tools (``get_email``, ``archive_message``,
``add_label``) verify label membership in their PRECHECK: it fetches the
message (a read-only GET — a precheck must not change state) and refuses
with ``OUT_OF_SCOPE`` before the approval gate, and the executor re-runs
it right before an approved handler, so approval never widens the scope.
A 401/403 on that fetch (a revoked grant) is the ``EXPIRED`` reason.
The handler of ``get_email`` then fetches the message once more to return
it (the precheck/handler contract has no channel to pass data through).
"""

import base64
import json
import logging
import time
from email.message import EmailMessage as MimeMessage
from email.utils import getaddresses
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import (
    CredentialsKeyFormatError,
    DecryptionError,
    decrypt_credentials,
)
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.models import UserConnection
from app.modules.connections.service import create_or_update
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.provider import (
    GmailAuthenticationError,
    GmailProvider,
    get_provider,
)
from app.modules.google_mail.scope import ALL_SCOPE, ScopeSelection, get_effective_scope
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.tools.exceptions import ToolPrecheckError
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolSpec,
)

logger = logging.getLogger(__name__)

MODULE_KEY = "google_mail"

#: Precheck reasons (short, English, actionable — the model relays them).
NOT_CONFIGURED = (
    "google mail connection not configured; the user must connect Google and choose a "
    "mailbox scope in Settings > Tools first"
)
EXPIRED = "google mail connection expired; the user must reconnect Google in Settings > Tools"
OUT_OF_SCOPE = (
    "message is outside the mailbox scope the user chose; the user must widen the scope "
    "in Settings > Tools to access it"
)

#: Same refresh skew the calendar precheck uses (never refresh a live token).
EXPIRY_SKEW_SECONDS = 60

MAX_RESULTS_DEFAULT = 10
MAX_RESULTS_LIMIT = 50
MAX_RECIPIENTS = 20
MAX_SUBJECT_LENGTH = 998  # RFC 5322 line-length budget for a subject.
MAX_BODY_LENGTH = 100_000
#: Cap of the plain-text body the tools hand the model (huge mails stay usable).
MAX_DECODED_BODY_CHARS = 20_000

#: The label ``archive_message`` removes (Gmail's system inbox label).
INBOX_LABEL = "INBOX"

#: System labels ``add_label`` never applies: Gmail purges Trash and Spam
#: after 30 days, so applying either one would delete mail (never in the MVP).
FORBIDDEN_LABELS = frozenset({"TRASH", "SPAM"})


# --- Input / output models ------------------------------------------------------


class SearchEmailsInput(BaseModel):
    """Typed, bounded input of ``search_emails`` (no tenant: it is implicit)."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, min_length=1, description="Free-text search.")
    max_results: int = Field(default=MAX_RESULTS_DEFAULT, ge=1, le=MAX_RESULTS_LIMIT)


class EmailSummary(BaseModel):
    """One search hit (the Gmail message resource, trimmed).

    Gmail's ``users.messagesList`` answers only ``id`` and ``threadId``;
    ``label_ids`` and ``snippet`` fill only when the API includes them.
    """

    id: str
    thread_id: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    snippet: str | None = None


class SearchEmailsOutput(BaseModel):
    """The messages the (scope-filtered) search returned."""

    messages: list[EmailSummary]


class GetEmailInput(BaseModel):
    """Typed input of ``get_email``."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)


class EmailMessage(BaseModel):
    """One full message flattened for the model (``format=full``, trimmed)."""

    id: str
    thread_id: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    snippet: str | None = None
    subject: str | None = None
    sender: str | None = None
    to: list[str] = Field(default_factory=list)
    date: str | None = None
    body: str | None = None


class GetEmailOutput(BaseModel):
    """The one message ``get_email`` read inside the scope."""

    message: EmailMessage


class ListLabelsInput(BaseModel):
    """Typed input of ``list_labels``: nothing but the implicit caller."""

    model_config = ConfigDict(extra="forbid")


class MailLabel(BaseModel):
    """One mailbox label: its API id and display name."""

    id: str
    name: str


class ListLabelsOutput(BaseModel):
    """Every label of the connected mailbox (metadata only)."""

    labels: list[MailLabel]


class SendEmailInput(BaseModel):
    """Typed input of ``send_email`` (RFC 822 recipients, one message)."""

    model_config = ConfigDict(extra="forbid")

    to: list[str] = Field(min_length=1, max_length=MAX_RECIPIENTS)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_LENGTH)
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH)
    cc: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    bcc: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    thread_id: str | None = Field(default=None, min_length=1)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def _recipients_are_addresses(cls, value: list[str]) -> list[str]:
        for address in value:
            if not address.strip() or "@" not in address:
                raise ValueError("recipient entries must be non-empty email addresses")
        return value


class SendEmailOutput(BaseModel):
    """What the model needs to confirm a sent message."""

    id: str
    thread_id: str | None = None


class ArchiveMessageInput(BaseModel):
    """Typed input of ``archive_message``."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)


class ArchiveMessageOutput(BaseModel):
    """Confirmation of one archive."""

    archived: bool
    message_id: str


class AddLabelInput(BaseModel):
    """Typed input of ``add_label`` (the label may be any; the message must be in scope)."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    label_id: str = Field(min_length=1)

    @field_validator("label_id")
    @classmethod
    def _label_does_not_delete(cls, value: str) -> str:
        if value.strip().upper() in FORBIDDEN_LABELS:
            raise ValueError("TRASH and SPAM cannot be applied: deleting mail is not supported")
        return value


class AddLabelOutput(BaseModel):
    """Confirmation of one label application."""

    labeled: bool
    message_id: str
    label_id: str


# --- Connection precheck (calendar-style, scope-aware) --------------------------


async def _load_connection(
    session: AsyncSession, *, tenant_id: int, user_id: int
) -> UserConnection | None:
    """The user's newest ``google_mail`` connection row (``None`` when absent).

    Same "newest wins" rule as the onboarding tester bridge: the MVP stores
    one provider row per module, and a reconnection reuses that row.
    """
    result = await session.execute(
        select(UserConnection)
        .where(
            UserConnection.tenant_id == tenant_id,
            UserConnection.user_id == user_id,
            UserConnection.module_key == MODULE_KEY,
        )
        .order_by(UserConnection.created_at.desc(), UserConnection.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _decrypt_blob(credentials_key: str | None, row: UserConnection) -> dict[str, Any] | None:
    """Decrypt the row's credentials JSON; ``None`` when absent or unreadable.

    A missing key, a malformed key, corrupted ciphertext, or a non-object
    blob all collapse to ``None`` — the caller maps that to the same
    "not configured" precheck and never echoes the underlying error.
    """
    if credentials_key is None or row.credentials_encrypted is None:
        return None
    try:
        blob: Any = json.loads(decrypt_credentials(credentials_key, row.credentials_encrypted))
    except (CredentialsKeyFormatError, DecryptionError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


def _is_expired(expires_at: Any) -> bool:
    """True when ``expires_at`` (epoch seconds) is a number past the skew.

    A blob without a parsable ``expires_at`` is treated as *not* expired:
    the provider's own 401 is the fallback (never guess a refresh).
    """
    if not isinstance(expires_at, (int, float)):
        return False
    return time.time() >= float(expires_at) - EXPIRY_SKEW_SECONDS


def _google_client_credentials(settings: Settings | None) -> tuple[str, str] | None:
    """The configured Google client id/secret, or ``None`` when unusable."""
    if settings is None:
        return None
    client_id = settings.google_client_id.get_secret_value() if settings.google_client_id else ""
    client_secret = (
        settings.google_client_secret.get_secret_value() if settings.google_client_secret else ""
    )
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def _mail_catalog() -> ModuleCatalog:
    """A throwaway catalog holding only ``google_mail`` for the refresh upsert.

    ``create_or_update`` validates its ``module_key`` against a catalog; the
    precheck brings its own one-module view instead of depending on the
    process-wide default (same pattern as the calendar/scope refresh).
    """
    catalog = ModuleCatalog()
    catalog.register(GOOGLE_MAIL_MANIFEST)
    return catalog


async def _refresh_and_persist(
    context: ToolContext, *, row: UserConnection, refresh_token: str, blob: dict[str, Any]
) -> str:
    """Refresh the access token and store the new blob on the same connection.

    Raises:
        ToolPrecheckError: the refresh was refused or impossible (no client
            credentials configured) — the "reconnect" reason.
    """
    credentials = _google_client_credentials(context.settings)
    if credentials is None:
        raise ToolPrecheckError(EXPIRED)
    client_id, client_secret = credentials
    try:
        tokens = await oauth_service.refresh_access_token(
            refresh_token=refresh_token, client_id=client_id, client_secret=client_secret
        )
    except (oauth_service.TokenExchangeError, httpx.RequestError):
        logger.warning("google_mail token refresh failed; the user must reconnect", exc_info=True)
        raise ToolPrecheckError(EXPIRED) from None
    refreshed: dict[str, Any] = {
        "access_token": tokens.access_token,
        # Google may omit the refresh token on refresh: keep the stored one.
        "refresh_token": tokens.refresh_token or refresh_token,
        "scopes": list(tokens.scopes) or list(blob.get("scopes") or []),
    }
    if tokens.expires_in is not None:
        refreshed["expires_at"] = int(time.time()) + tokens.expires_in
    new_blob = json.dumps(refreshed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    await create_or_update(
        context.session,
        catalog=_mail_catalog(),
        credentials_key=context.credentials_key,
        caller=context.user,
        module_key=MODULE_KEY,
        provider=row.provider,
        credentials=new_blob,
    )
    return tokens.access_token


async def _scoped_connection(context: ToolContext) -> tuple[ScopeSelection, UserConnection, str]:
    """The user's persisted scope, connection row, and a usable access token.

    The G2 accessor answers the selection (or ``None`` — NOT_CONFIGURED, the
    same semantic it was built for); then the calendar-style connection
    load, decrypt, and on-demand refresh.

    Raises:
        ToolPrecheckError: no selection, no connection, no blob, an
            unusable ``credentials_key``, an unreadable blob, or no access
            token (all ``NOT_CONFIGURED``), or an expired token that could
            not be refreshed (``EXPIRED``). Never raises anything else for
            connection problems — the executor masks those as 500s.
    """
    selection = await get_effective_scope(
        context.session, tenant_id=context.tenant_id, user_id=context.user.id
    )
    if selection is None:
        raise ToolPrecheckError(NOT_CONFIGURED)
    row = await _load_connection(
        context.session, tenant_id=context.tenant_id, user_id=context.user.id
    )
    if row is None:
        raise ToolPrecheckError(NOT_CONFIGURED)
    blob = _decrypt_blob(context.credentials_key, row)
    if blob is None:
        raise ToolPrecheckError(NOT_CONFIGURED)
    access_token = blob.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ToolPrecheckError(NOT_CONFIGURED)
    if not _is_expired(blob.get("expires_at")):
        return selection, row, access_token
    refresh_token = blob.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ToolPrecheckError(EXPIRED)
    new_token = await _refresh_and_persist(context, row=row, refresh_token=refresh_token, blob=blob)
    return selection, row, new_token


def _provider_for(row: UserConnection, access_token: str) -> GmailProvider:
    """Build the per-user provider from the row's registered factory."""
    factory = get_provider(row.provider)
    return factory(access_token=access_token)


def _message_label_ids(message: Any) -> list[str]:
    """The message's ``labelIds`` as a list of strings (malformed → empty)."""
    raw = message.get("labelIds") if isinstance(message, dict) else None
    if not isinstance(raw, list):
        return []
    return [label for label in raw if isinstance(label, str)]


def _in_scope(selection: ScopeSelection, label_ids: list[str]) -> bool:
    """True when the message may be read/acted upon under ``selection``."""
    return selection.scope_id == ALL_SCOPE or selection.scope_id in label_ids


async def google_mail_precheck(context: ToolContext, validated_input: BaseModel) -> None:
    """Verify the scope selection and a live connection before any handler runs.

    Loads the persisted scope (G2 accessor) and decrypts the stored
    credentials, refreshing an expired access token when possible. It never
    builds a provider — a refused call can never reach the external API.
    """
    await _scoped_connection(context)


async def _scoped_message_precheck(context: ToolContext, validated_input: BaseModel) -> None:
    """Precheck of the message-targeting tools: connection + label membership.

    Fetches the message (read-only — a precheck must not change state) and
    refuses with ``OUT_OF_SCOPE`` when its ``labelIds`` do not contain the
    selected label (``ALL`` skips the check). The executor re-runs this
    right before an approved handler, so an approval grant never widens
    the scope.
    """
    selection, row, access_token = await _scoped_connection(context)
    message_id = validated_input.message_id
    provider = _provider_for(row, access_token)
    try:
        message = await provider.get_message(message_id)
    except GmailAuthenticationError:
        # Gmail refused a token the clock still considers live (revoked grant).
        logger.warning("google_mail rejected the stored token; the user must reconnect")
        raise ToolPrecheckError(EXPIRED) from None
    if not _in_scope(selection, _message_label_ids(message)):
        raise ToolPrecheckError(OUT_OF_SCOPE)


# --- MIME building (stdlib email) ------------------------------------------------


def _build_raw_mime(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> str:
    """One RFC 822 message, base64url-encoded for ``users.messagesSend``.

    No ``From`` header: Gmail sets it to the authenticated user (the
    connected mailbox), so the tool never guesses the sender address.
    """
    message = MimeMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


# --- Message flattening ----------------------------------------------------------


def _map_summary(item: dict[str, Any]) -> EmailSummary:
    """Flatten one ``users.messagesList`` entry into an :class:`EmailSummary`."""
    thread_id = item.get("threadId")
    snippet = item.get("snippet")
    return EmailSummary(
        id=str(item.get("id", "")),
        thread_id=thread_id if isinstance(thread_id, str) else None,
        label_ids=_message_label_ids(item),
        snippet=snippet if isinstance(snippet, str) else None,
    )


def _decode_body_data(data: str) -> str:
    """base64url-decode one MIME part body (never raises into the model)."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _plain_text_body(part: Any) -> str | None:
    """The first ``text/plain`` body inside a ``format=full`` MIME tree."""
    if not isinstance(part, dict):
        return None
    mime_type = part.get("mimeType")
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    data = body.get("data")
    if mime_type == "text/plain" and isinstance(data, str) and data:
        return _decode_body_data(data)[:MAX_DECODED_BODY_CHARS]
    for child in part.get("parts") or []:
        found = _plain_text_body(child)
        if found is not None:
            return found
    return None


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Lowercased header name → value of one ``format=full`` payload."""
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return {}
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in headers
        if isinstance(header, dict)
    }


def _map_message(item: dict[str, Any]) -> EmailMessage:
    """Flatten one full Gmail message resource into an :class:`EmailMessage`."""
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    headers = _header_map(payload)
    thread_id = item.get("threadId")
    snippet = item.get("snippet")
    return EmailMessage(
        id=str(item.get("id", "")),
        thread_id=thread_id if isinstance(thread_id, str) else None,
        label_ids=_message_label_ids(item),
        snippet=snippet if isinstance(snippet, str) else None,
        subject=headers.get("subject") or None,
        sender=headers.get("from") or None,
        to=[address for _, address in getaddresses([headers.get("to", "")])],
        date=headers.get("date") or None,
        body=_plain_text_body(payload),
    )


# --- Handlers --------------------------------------------------------------------


async def search_emails_handler(
    context: ToolContext, validated_input: SearchEmailsInput
) -> SearchEmailsOutput:
    """Search inside the scope: ``labelIds`` always (unless ``ALL``)."""
    selection, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    label_ids = None if selection.scope_id == ALL_SCOPE else [selection.scope_id]
    items = await provider.list_messages(
        label_ids=label_ids, query=validated_input.query, max_results=validated_input.max_results
    )
    return SearchEmailsOutput(
        messages=[_map_summary(item) for item in items if isinstance(item, dict)]
    )


async def get_email_handler(context: ToolContext, validated_input: GetEmailInput) -> GetEmailOutput:
    """Return one full message (its membership was verified by the precheck)."""
    _, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    message = await provider.get_message(validated_input.message_id)
    return GetEmailOutput(message=_map_message(message))


async def list_labels_handler(
    context: ToolContext, validated_input: ListLabelsInput
) -> ListLabelsOutput:
    """Return every mailbox label as id + display name (metadata only)."""
    _, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    labels = await provider.list_labels()
    return ListLabelsOutput(
        labels=[
            MailLabel(id=str(label.get("id")), name=str(label.get("name")))
            for label in labels
            if isinstance(label, dict) and label.get("id") and label.get("name")
        ]
    )


async def send_email_handler(
    context: ToolContext, validated_input: SendEmailInput
) -> SendEmailOutput:
    """Send one MIME message (exempt from the label check, never from approval)."""
    _, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    raw = _build_raw_mime(
        to=validated_input.to,
        subject=validated_input.subject,
        body=validated_input.body,
        cc=validated_input.cc,
        bcc=validated_input.bcc,
    )
    payload = await provider.send_message(raw_mime_b64url=raw, thread_id=validated_input.thread_id)
    thread_id = payload.get("threadId")
    return SendEmailOutput(
        id=str(payload.get("id", "")),
        thread_id=thread_id if isinstance(thread_id, str) else None,
    )


async def archive_message_handler(
    context: ToolContext, validated_input: ArchiveMessageInput
) -> ArchiveMessageOutput:
    """Remove ``INBOX`` from one in-scope message (membership: the precheck)."""
    _, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    await provider.modify_message(
        message_id=validated_input.message_id,
        add_label_ids=None,
        remove_label_ids=[INBOX_LABEL],
    )
    return ArchiveMessageOutput(archived=True, message_id=validated_input.message_id)


async def add_label_handler(context: ToolContext, validated_input: AddLabelInput) -> AddLabelOutput:
    """Add one label to one in-scope message (membership: the precheck)."""
    _, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    await provider.modify_message(
        message_id=validated_input.message_id,
        add_label_ids=[validated_input.label_id],
        remove_label_ids=None,
    )
    return AddLabelOutput(
        labeled=True,
        message_id=validated_input.message_id,
        label_id=validated_input.label_id,
    )


# --- ToolSpecs -------------------------------------------------------------------


SEARCH_EMAILS = ToolSpec(
    name="search_emails",
    description=(
        "Search the user's Gmail messages inside the allowed mailbox scope: optional "
        "free-text query plus a bounded result count; each result carries the message "
        "id and thread id (read one with get_email). Reading never asks for confirmation."
    ),
    kind=ToolKind.EXTRACT,
    approval_policy=ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
    required_role=UserRole.VIEWER,
    handler=search_emails_handler,
    input_model=SearchEmailsInput,
    output_model=SearchEmailsOutput,
    precheck=google_mail_precheck,
    module=MODULE_KEY,
)

GET_EMAIL = ToolSpec(
    name="get_email",
    description=(
        "Read one Gmail message in full (subject, sender, recipients, date, snippet, "
        "plain-text body, labels); refused when the message is outside the mailbox "
        "scope the user chose."
    ),
    kind=ToolKind.EXTRACT,
    approval_policy=ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
    required_role=UserRole.VIEWER,
    handler=get_email_handler,
    input_model=GetEmailInput,
    output_model=GetEmailOutput,
    precheck=_scoped_message_precheck,
    module=MODULE_KEY,
)

LIST_LABELS = ToolSpec(
    name="list_labels",
    description=(
        "List the connected mailbox's labels, each with its id and display name "
        "(system and user labels)."
    ),
    kind=ToolKind.EXTRACT,
    approval_policy=ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
    required_role=UserRole.VIEWER,
    handler=list_labels_handler,
    input_model=ListLabelsInput,
    output_model=ListLabelsOutput,
    precheck=google_mail_precheck,
    module=MODULE_KEY,
)

SEND_EMAIL = ToolSpec(
    name="send_email",
    description=(
        "Send one email from the connected mailbox (recipients, subject, body; "
        "optional cc/bcc and a thread id when replying). The user is always asked to "
        "confirm before it is sent."
    ),
    kind=ToolKind.ACTION,
    approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
    required_role=UserRole.OPERATOR,
    handler=send_email_handler,
    input_model=SendEmailInput,
    output_model=SendEmailOutput,
    precheck=google_mail_precheck,
    module=MODULE_KEY,
)

ARCHIVE_MESSAGE = ToolSpec(
    name="archive_message",
    description=(
        "Archive one Gmail message by removing it from the inbox; only messages "
        "inside the mailbox scope the user chose. The user is always asked to confirm."
    ),
    kind=ToolKind.ACTION,
    approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
    required_role=UserRole.OPERATOR,
    handler=archive_message_handler,
    input_model=ArchiveMessageInput,
    output_model=ArchiveMessageOutput,
    precheck=_scoped_message_precheck,
    module=MODULE_KEY,
)

ADD_LABEL = ToolSpec(
    name="add_label",
    description=(
        "Add one label to one Gmail message; only messages inside the mailbox scope "
        "the user chose. The user is always asked to confirm."
    ),
    kind=ToolKind.ACTION,
    approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
    required_role=UserRole.OPERATOR,
    handler=add_label_handler,
    input_model=AddLabelInput,
    output_model=AddLabelOutput,
    precheck=_scoped_message_precheck,
    module=MODULE_KEY,
)


def register_google_mail_tools(registry: ToolRegistry) -> None:
    """Register the six google_mail tools on ``registry``.

    Explicit entry point: importing this module registers nothing. The
    production fold (:func:`~app.modules.google_mail.registry.register_default_google_mail`,
    the reviewed google-mail G3 allowlist entry) calls it once at import
    time on ``default_registry``; tests call it against a LOCAL registry.
    """
    for spec in (SEARCH_EMAILS, GET_EMAIL, LIST_LABELS, SEND_EMAIL, ARCHIVE_MESSAGE, ADD_LABEL):
        registry.register(spec)


__all__ = [
    "ADD_LABEL",
    "ARCHIVE_MESSAGE",
    "EXPIRED",
    "GET_EMAIL",
    "LIST_LABELS",
    "MODULE_KEY",
    "NOT_CONFIGURED",
    "OUT_OF_SCOPE",
    "SEARCH_EMAILS",
    "SEND_EMAIL",
    "AddLabelInput",
    "AddLabelOutput",
    "ArchiveMessageInput",
    "ArchiveMessageOutput",
    "EmailMessage",
    "EmailSummary",
    "GetEmailInput",
    "GetEmailOutput",
    "ListLabelsInput",
    "ListLabelsOutput",
    "MailLabel",
    "SearchEmailsInput",
    "SearchEmailsOutput",
    "SendEmailInput",
    "SendEmailOutput",
    "register_google_mail_tools",
]
