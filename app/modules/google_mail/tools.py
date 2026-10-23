"""Google Mail chat tools (G3, google-mail M3): scoped reads and gated actions.

Seven ToolSpecs on module ``google_mail``, registered through
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
  does (risk #2). Each hit is then described by one ``format=metadata``
  read (subject, sender, date, snippet — G3.1), at most
  :data:`METADATA_CONCURRENCY` at once and in Gmail's order: a hit deleted
  meanwhile (404) is skipped, and a hit whose metadata lacks the selected
  label is dropped (defense in depth);
- ``get_email`` — ``EXTRACT`` / viewer, no approval: one full message,
  refused with the actionable ``OUT_OF_SCOPE`` reason when the message's
  ``labelIds`` do not contain the selected label (criterion 3's heart);
  ``ALL`` skips the check. The body is the first ``text/plain`` part, or —
  for HTML-only mail — the first ``text/html`` part converted to plain
  text with the stdlib parser (G8). Every part with a file name is listed
  as an attachment (part id, file name, type, size) — never Gmail's long,
  unstable ``attachmentId``;
- ``list_labels`` — ``EXTRACT`` / viewer, no approval: the mailbox's
  label ids + display names (metadata only);
- ``send_email`` — ``ACTION`` / operator, ``REQUIRE_APPROVAL``: builds a
  proper RFC 822 MIME (stdlib ``email``) whose ``From`` is the connected
  address (``users.getProfile``) plus ``Date`` and ``Message-ID``,
  base64url-encodes it, and sends it (``threadId`` when replying). Gmail
  does NOT fill a missing ``From`` (G8: such a send arrived as "(unknown
  sender)" with no ``SENT`` label), so no readable address means no send,
  and an answer without the ``SENT`` label is never reported as sent.
  With ``attachments`` (relative paths in the user's workspace folder) the
  precheck reads only their sizes and refuses more than
  :data:`MAX_SEND_BYTES` (Gmail's 25 MB, body plus files, before
  encoding); after approval the files are read through the chunked
  connector ops and the ``multipart/mixed`` message goes through the
  resumable upload. EXEMPT from the label-membership check — a new message
  belongs to no folder — but approval is ALWAYS required;
- ``archive_message`` — ``ACTION`` / operator, ``REQUIRE_APPROVAL``:
  ``modify`` removing ``INBOX``, only after verifying the message is
  inside the scope;
- ``add_label(message_id, label_id)`` — ``ACTION`` / operator,
  ``REQUIRE_APPROVAL``: ``modify`` adding a label, only after verifying
  the TARGET MESSAGE is inside the scope (the label being applied may be
  any label except ``TRASH``/``SPAM`` — the constraint is on the message
  acted upon);
- ``save_attachment(message_id, part_id, path)`` — ``ACTION`` / operator,
  ``REQUIRE_APPROVAL``: writes one attachment of an in-scope message,
  byte for byte, to a relative path in the user's workspace folder through
  the chunked connector ops (never over an existing file), up to
  :data:`MAX_DOWNLOAD_BYTES` (Gmail's largest receivable message).

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
The four actions also set ``requester_only_approval`` (G7): the handler
runs with the identity of whoever executes the approved call, so only the
requester may approve, reject, or execute it — another user of the tenant
would otherwise act on their own mailbox.

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
``send_email``'s precheck likewise reads the sender profile (read-only), so
a revoked grant answers ``EXPIRED`` before the approval gate and again
right before the approved handler, which reads the address once more.
"""

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage
from email.utils import format_datetime, getaddresses, make_msgid, parseaddr
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)
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
from app.modules.connectors.tools import checked_target, require_online
from app.modules.connectors.transfer import (
    read_workspace_file,
    workspace_file_size,
    write_workspace_file,
)
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.provider import (
    GmailAuthenticationError,
    GmailNotFoundError,
    GmailProvider,
    GmailProviderError,
    get_provider,
)
from app.modules.google_mail.scope import ALL_SCOPE, ScopeSelection, get_effective_scope
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.tools.exceptions import ToolPrecheckError, ToolRefusalError
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

ATTACHMENT_NOT_FOUND = (
    "attachment not found in this message; call get_email for its attachments and part ids"
)
ATTACHMENT_TOO_LARGE = "attachment exceeds Gmail's 70 MB limit and cannot be downloaded"
ATTACHMENTS_TOO_LARGE = (
    "body and attachments exceed Gmail's 25 MB sending limit; attach fewer or smaller files"
)

#: Same refresh skew the calendar precheck uses (never refresh a live token).
EXPIRY_SKEW_SECONDS = 60

MAX_RESULTS_DEFAULT = 10
MAX_RESULTS_LIMIT = 50
#: How many search hits ``search_emails`` reads (``format=metadata``) at once.
METADATA_CONCURRENCY = 10
MAX_RECIPIENTS = 20
MAX_SUBJECT_LENGTH = 998  # RFC 5322 line-length budget for a subject.
MAX_BODY_LENGTH = 100_000
#: Cap of the plain-text body the tools hand the model (huge mails stay usable).
MAX_DECODED_BODY_CHARS = 20_000
#: Cap of the HTML source parsed for an HTML-only body (bounds the parser's work).
MAX_HTML_SOURCE_CHARS = 1_000_000
#: Gmail's sending limit: message content plus attachments, before encoding.
MAX_SEND_BYTES = 25_000_000
#: Gmail's largest receivable message (70 MB after encoding): no attachment is larger.
MAX_DOWNLOAD_BYTES = 70_000_000
MAX_ATTACHMENTS = 20

#: The label ``archive_message`` removes (Gmail's system inbox label).
INBOX_LABEL = "INBOX"

#: The label Gmail puts on a message it actually sent (``users.messagesSend`` answer).
SENT_LABEL = "SENT"

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
    """One search hit, described from its ``format=metadata`` read (G3.1).

    Gmail's ``users.messagesList`` answers only ``id`` and ``threadId``, so
    the search reads each hit's labels, snippet, and ``Subject``/``From``/
    ``Date`` headers: enough to describe or pick the message. ``get_email``
    is only needed for the full body.
    """

    id: str
    thread_id: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    subject: str | None = None
    sender: str | None = None
    date: str | None = None
    snippet: str | None = None


class SearchEmailsOutput(BaseModel):
    """The messages the (scope-filtered) search returned."""

    messages: list[EmailSummary]


class GetEmailInput(BaseModel):
    """Typed input of ``get_email``."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)


class EmailAttachment(BaseModel):
    """One attachment of a message: what ``save_attachment`` needs to fetch it."""

    part_id: str
    filename: str
    mime_type: str | None = None
    size: int | None = None


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
    attachments: list[EmailAttachment] = Field(default_factory=list)


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
    attachments: list[str] = Field(
        default_factory=list,
        max_length=MAX_ATTACHMENTS,
        description="Relative paths of files in the user's workspace folder.",
    )

    @field_validator("to", "cc", "bcc")
    @classmethod
    def _recipients_are_addresses(cls, value: list[str]) -> list[str]:
        for address in value:
            if not address.strip() or "@" not in address:
                raise ValueError("recipient entries must be non-empty email addresses")
        return value

    @field_validator("attachments")
    @classmethod
    def _attachments_are_paths(cls, value: list[str]) -> list[str]:
        if any(not path.strip() for path in value):
            raise ValueError("attachment entries must be workspace file paths")
        return value

    @model_serializer(mode="wrap")
    def _omit_empty_attachments(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Dump a send without attachments exactly as before they existed.

        An approval request stores this dump and binds it by hash
        (``action_key``), so a request created by the previous release must
        still match after the upgrade.
        """
        data = handler(self)
        if not self.attachments:
            data.pop("attachments", None)
        return data


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


class SaveAttachmentInput(BaseModel):
    """Typed input of ``save_attachment`` (the part id comes from ``get_email``)."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    part_id: str = Field(min_length=1)
    path: str = Field(
        min_length=1, description="Relative destination path in the user's workspace folder."
    )


class SaveAttachmentOutput(BaseModel):
    """Where the attachment was written and how many bytes it holds."""

    path: str
    bytes: int
    filename: str


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
    await _in_scope_message(context, validated_input.message_id)


async def _in_scope_message(context: ToolContext, message_id: str) -> dict[str, Any]:
    """Fetch one message for a precheck, refusing it outside the scope (see above)."""
    selection, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    try:
        message = await provider.get_message(message_id)
    except GmailAuthenticationError:
        # Gmail refused a token the clock still considers live (revoked grant).
        logger.warning("google_mail rejected the stored token; the user must reconnect")
        raise ToolPrecheckError(EXPIRED) from None
    if not _in_scope(selection, _message_label_ids(message)):
        raise ToolPrecheckError(OUT_OF_SCOPE)
    return message


def _is_plain_address(value: Any) -> bool:
    """True for one bare ``local@domain`` address (no display name, spaces, or controls).

    The profile address becomes the ``From`` header verbatim, so anything
    that parses differently from itself (a display name, a second ``@``,
    an injected header line) is refused rather than repaired.
    """
    if not isinstance(value, str) or not value:
        return False
    if any(char.isspace() or not char.isprintable() for char in value):
        return False
    if parseaddr(value) != ("", value):
        return False
    local, _, domain = value.rpartition("@")
    return bool(local) and bool(domain) and "@" not in local


async def _sender_address(provider: GmailProvider) -> str:
    """The connected mailbox's address (``users.getProfile``): the message's ``From``.

    Raises:
        ToolPrecheckError: Gmail rejected the token (401/403) — ``EXPIRED``
            (from a precheck; inside a handler the executor masks it).
        GmailProviderError: the profile carries no single bare address (the
            executor masks it as ``tool_failed``); other provider and
            transport errors propagate the same way. Nothing is sent.
    """
    try:
        profile = await provider.get_profile()
    except GmailAuthenticationError:
        logger.warning("google_mail rejected the stored token; the user must reconnect")
        raise ToolPrecheckError(EXPIRED) from None
    address = profile.get("emailAddress") if isinstance(profile, dict) else None
    if not _is_plain_address(address):
        raise GmailProviderError("Gmail profile has no usable emailAddress")
    return address


async def _send_email_precheck(context: ToolContext, validated_input: BaseModel) -> None:
    """Precheck of ``send_email``: connection + a readable sender address.

    Reads the profile (read-only — a precheck must not change state) so a
    revoked grant answers ``EXPIRED`` before the user is asked to approve a
    send that cannot happen, and again right before the approved handler.
    No label check: a new message belongs to no folder.
    """
    _, row, access_token = await _scoped_connection(context)
    await _sender_address(_provider_for(row, access_token))
    if validated_input.attachments:
        await _check_attachment_budget(context, validated_input)


async def _check_attachment_budget(context: ToolContext, validated_input: SendEmailInput) -> None:
    """Refuse attachments that are missing or exceed :data:`MAX_SEND_BYTES` with the body.

    Only sizes are read (``file_info``): the content waits for the approval.
    """
    require_online(context)
    total = len(validated_input.body.encode("utf-8"))
    for path in validated_input.attachments:
        total += await workspace_file_size(context, path)
        if total > MAX_SEND_BYTES:
            raise ToolPrecheckError(ATTACHMENTS_TOO_LARGE)


async def _save_attachment_precheck(
    context: ToolContext, validated_input: SaveAttachmentInput
) -> None:
    """Precheck of ``save_attachment``: scope, the attachment part, and the target.

    The message must be inside the scope (as for the other message tools)
    and hold the requested attachment within :data:`MAX_DOWNLOAD_BYTES`;
    the user's connector must be online and the path a write target inside
    the granted folder. Nothing is downloaded or written before approval.
    """
    message = await _in_scope_message(context, validated_input.message_id)
    part = _find_attachment(message, validated_input.part_id)
    if part is None:
        raise ToolPrecheckError(ATTACHMENT_NOT_FOUND)
    if _attachment_size(part) > MAX_DOWNLOAD_BYTES:
        raise ToolPrecheckError(ATTACHMENT_TOO_LARGE)
    require_online(context)
    checked_target(validated_input.path)


# --- MIME building (stdlib email) ------------------------------------------------


def _build_mime(
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> MimeMessage:
    """One RFC 822 message from the connected address.

    ``From`` is ``sender`` — the connected address from ``users.getProfile``:
    Gmail does not fill a missing ``From`` (G8, the "(unknown sender)" send).
    ``Date`` is the current UTC time and ``Message-ID`` a fresh id on the
    sender's domain, so the message is well formed without relying on Gmail.
    """
    message = MimeMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    message.set_content(body)
    return message


def _build_raw_mime(**fields: Any) -> str:
    """:func:`_build_mime`'s message, base64url-encoded for ``users.messagesSend``."""
    return base64.urlsafe_b64encode(_build_mime(**fields).as_bytes()).decode("ascii")


async def _attach_workspace_files(
    context: ToolContext, message: MimeMessage, validated_input: SendEmailInput
) -> None:
    """Read each attachment from the workspace (after approval) and add it to ``message``.

    What the body leaves of :data:`MAX_SEND_BYTES` bounds every read, so a
    file that grew since the precheck is refused before it is read in full
    and nothing is sent. The type comes from the file extension.
    """
    budget = MAX_SEND_BYTES - len(validated_input.body.encode("utf-8"))
    for path in validated_input.attachments:
        data = await read_workspace_file(
            context, path, max_bytes=budget, too_large_message=ATTACHMENTS_TOO_LARGE
        )
        budget -= len(data)
        filename = PurePosixPath(path.replace("\\", "/")).name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


# --- Message flattening ----------------------------------------------------------


def _decode_body_bytes(data: str) -> bytes:
    """base64url-decode one MIME part body, padded or not."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_body_data(data: str) -> str:
    """base64url-decode one MIME part body as text (never raises into the model)."""
    return _decode_body_bytes(data).decode("utf-8", errors="replace")


def _part_body(part: dict[str, Any]) -> dict[str, Any]:
    body = part.get("body")
    return body if isinstance(body, dict) else {}


def _attachment_parts(part: Any) -> list[dict[str, Any]]:
    """Every part with a file name and a part id inside a ``format=full`` tree, in order."""
    if not isinstance(part, dict):
        return []
    found = []
    filename, part_id = part.get("filename"), part.get("partId")
    if isinstance(filename, str) and filename and isinstance(part_id, str) and part_id:
        found.append(part)
    children = part.get("parts")
    for child in children if isinstance(children, list) else []:
        found.extend(_attachment_parts(child))
    return found


def _find_attachment(message: Any, part_id: str) -> dict[str, Any] | None:
    payload = message.get("payload") if isinstance(message, dict) else None
    return next((part for part in _attachment_parts(payload) if part["partId"] == part_id), None)


def _attachment_size(part: dict[str, Any]) -> int:
    """Gmail's byte count of the part data (encoding notwithstanding); 0 when absent."""
    size = _part_body(part).get("size")
    return size if isinstance(size, int) and not isinstance(size, bool) else 0


def _map_attachment(part: dict[str, Any]) -> EmailAttachment:
    mime_type = part.get("mimeType")
    return EmailAttachment(
        part_id=part["partId"],
        filename=part["filename"],
        mime_type=mime_type if isinstance(mime_type, str) else None,
        size=_attachment_size(part),
    )


def _first_part_text(part: Any, mime_type: str) -> str | None:
    """The decoded body of the first ``mime_type`` part inside a ``format=full`` tree."""
    if not isinstance(part, dict):
        return None
    part_type = part.get("mimeType")
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    data = body.get("data")
    if isinstance(part_type, str) and part_type.lower() == mime_type:
        if isinstance(data, str) and data:
            return _decode_body_data(data)
    children = part.get("parts")
    for child in children if isinstance(children, list) else []:
        found = _first_part_text(child, mime_type)
        if found is not None:
            return found
    return None


class _HtmlTextExtractor(HTMLParser):
    """Readable plain text out of one HTML body (stdlib parser, no rendering).

    Drops ``head``/``title``/``script``/``style`` content, turns block-level
    tags into line breaks (paragraphs and headings into a blank line),
    separates table cells with a space, and collapses source whitespace the
    way a browser does. Entities are unescaped by ``convert_charrefs``.
    """

    _SKIPPED = frozenset({"head", "title", "script", "style"})
    _PARAGRAPHS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})
    _LINES = frozenset({"div", "li", "tr", "ul", "ol", "table", "hr"})
    _CELLS = frozenset({"td", "th"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._trailing_newlines = 0

    def _break(self, count: int) -> None:
        """End the current text with at least ``count`` newlines (none before any text)."""
        if self._chunks and self._trailing_newlines < count:
            self._chunks.append("\n" * (count - self._trailing_newlines))
            self._trailing_newlines = count

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self._skip_depth = 0  # an unclosed <head> must not swallow the body
        elif tag in self._SKIPPED:
            self._skip_depth += 1
        elif self._skip_depth:
            return
        elif tag == "br":
            self._chunks.append("\n")
            self._trailing_newlines += 1
        elif tag in self._PARAGRAPHS:
            self._break(2)
        elif tag in self._LINES:
            self._break(1)
        elif tag in self._CELLS and self._chunks:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth:
            return
        elif tag in self._PARAGRAPHS:
            self._break(2)
        elif tag in self._LINES:
            self._break(1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            if self._chunks and not self._trailing_newlines:
                self._chunks.append(" ")  # keeps "<b>A</b> <i>B</i>" apart
            return
        self._chunks.append(text)
        self._trailing_newlines = 0

    def text(self) -> str:
        """The collected text: lines trimmed, at most one blank line in a row."""
        lines = [re.sub(r" {2,}", " ", line).strip() for line in "".join(self._chunks).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _html_to_text(html: str) -> str:
    """Convert one HTML body to readable plain text (bounded source, stdlib only)."""
    extractor = _HtmlTextExtractor()
    extractor.feed(html[:MAX_HTML_SOURCE_CHARS])
    extractor.close()
    return extractor.text()


def _message_body(payload: Any) -> str | None:
    """The body the model reads: ``text/plain`` first, else the HTML as text.

    Many mails ship only ``text/html`` (G8): without the fallback they reached
    the model with no body at all. Either way the text is capped at
    :data:`MAX_DECODED_BODY_CHARS`; an HTML body without text is ``None``.
    """
    plain = _first_part_text(payload, "text/plain")
    if plain is not None:
        return plain[:MAX_DECODED_BODY_CHARS]
    html = _first_part_text(payload, "text/html")
    if html is None:
        return None
    return _html_to_text(html)[:MAX_DECODED_BODY_CHARS] or None


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Lowercased header name → value of one ``format=full``/``metadata`` payload."""
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return {}
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in headers
        if isinstance(header, dict)
    }


def _map_summary(item: dict[str, Any]) -> EmailSummary:
    """Flatten one ``format=metadata`` message resource into an :class:`EmailSummary`."""
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    headers = _header_map(payload)
    thread_id = item.get("threadId")
    snippet = item.get("snippet")
    return EmailSummary(
        id=str(item.get("id", "")),
        thread_id=thread_id if isinstance(thread_id, str) else None,
        label_ids=_message_label_ids(item),
        subject=headers.get("subject") or None,
        sender=headers.get("from") or None,
        date=headers.get("date") or None,
        snippet=snippet if isinstance(snippet, str) else None,
    )


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
        body=_message_body(payload),
        attachments=[_map_attachment(part) for part in _attachment_parts(payload)],
    )


# --- Handlers --------------------------------------------------------------------


async def _read_hits_metadata(
    provider: GmailProvider, message_ids: list[str]
) -> list[dict[str, Any] | None]:
    """Read every hit's metadata, at most :data:`METADATA_CONCURRENCY` at once.

    The answers keep ``message_ids``' order (Gmail's ranking) whatever order
    the reads finish in; ``None`` marks a hit deleted between the list and
    its read (404). Any other failure aborts the whole search: the pending
    reads are cancelled and the error reaches the executor's masked
    ``tool_failed`` (never a 500, never a token leak).
    """
    semaphore = asyncio.Semaphore(METADATA_CONCURRENCY)

    async def read(message_id: str) -> dict[str, Any] | None:
        async with semaphore:
            try:
                return await provider.get_message_metadata(message_id)
            except GmailNotFoundError:
                return None

    reads = [asyncio.create_task(read(message_id)) for message_id in message_ids]
    try:
        return await asyncio.gather(*reads)
    except Exception:
        for pending in reads:
            pending.cancel()
        raise


async def search_emails_handler(
    context: ToolContext, validated_input: SearchEmailsInput
) -> SearchEmailsOutput:
    """Search inside the scope (``labelIds`` unless ``ALL``), then describe each hit.

    One ``format=metadata`` read per hit fills subject, sender, date, and
    snippet (G3.1). A hit whose metadata lacks the selected label is dropped:
    ``labelIds`` already filtered the list, so this defense costs one check.
    """
    selection, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    label_ids = None if selection.scope_id == ALL_SCOPE else [selection.scope_id]
    items = await provider.list_messages(
        label_ids=label_ids, query=validated_input.query, max_results=validated_input.max_results
    )
    message_ids = [
        item["id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]
    summaries: list[EmailSummary] = []
    for hit in await _read_hits_metadata(provider, message_ids):
        if hit is None:
            continue  # deleted between the list and its read
        if not _in_scope(selection, _message_label_ids(hit)):
            logger.warning("google_mail search dropped a hit outside the selected label")
            continue
        summaries.append(_map_summary(hit))
    return SearchEmailsOutput(messages=summaries)


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
    """Send one MIME message from the connected address (label-exempt, never approval-exempt).

    With attachments, the workspace files are read now (after approval) and
    the ``multipart/mixed`` message goes through the resumable upload;
    without them, the JSON ``raw`` send is unchanged. The sender is read again
    here (the precheck cannot pass it through the frozen context); without a
    usable address nothing is sent. Gmail's answer must carry the ``SENT``
    label: the G8 send that arrived as "(unknown sender)" came back with no
    ``labelIds`` at all, so an answer without ``SENT`` is a masked failure,
    never a reported success.
    """
    _, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    sender = await _sender_address(provider)
    message = _build_mime(
        sender=sender,
        to=validated_input.to,
        subject=validated_input.subject,
        body=validated_input.body,
        cc=validated_input.cc,
        bcc=validated_input.bcc,
    )
    if validated_input.attachments:
        await _attach_workspace_files(context, message, validated_input)
        # A JSON ``raw`` body is no place for 25 MB: the resumable upload takes it.
        payload = await provider.send_message_upload(
            mime=message.as_bytes(), thread_id=validated_input.thread_id
        )
    else:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        payload = await provider.send_message(
            raw_mime_b64url=raw, thread_id=validated_input.thread_id
        )
    labels = payload.get("labelIds")
    if not isinstance(labels, list) or SENT_LABEL not in labels:
        logger.warning("google_mail send answered without the SENT label")
        raise GmailProviderError("Gmail did not mark the message as sent")
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


async def save_attachment_handler(
    context: ToolContext, validated_input: SaveAttachmentInput
) -> SaveAttachmentOutput:
    """Write one attachment of an in-scope message into the workspace (membership: the precheck).

    An inline part carries its bytes in ``body.data``; any other is fetched
    with ``users.messages.attachments.get``. The chunked connector write
    never replaces an existing file and aborts its upload on failure.
    """
    _, row, access_token = await _scoped_connection(context)
    provider = _provider_for(row, access_token)
    message = await provider.get_message(validated_input.message_id)
    part = _find_attachment(message, validated_input.part_id)
    if part is None:
        raise ToolRefusalError(ATTACHMENT_NOT_FOUND)
    body = _part_body(part)
    attachment_id = body.get("attachmentId")
    if isinstance(attachment_id, str) and attachment_id:
        data = await provider.get_attachment(validated_input.message_id, attachment_id)
    else:
        inline = body.get("data")
        data = _decode_body_bytes(inline) if isinstance(inline, str) else b""
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ToolRefusalError(ATTACHMENT_TOO_LARGE)
    result = await write_workspace_file(context, validated_input.path, data)
    return SaveAttachmentOutput(
        path=str(result["path"]), bytes=len(data), filename=part["filename"]
    )


# --- ToolSpecs -------------------------------------------------------------------


SEARCH_EMAILS = ToolSpec(
    name="search_emails",
    description=(
        "Search the user's Gmail messages inside the allowed mailbox scope: optional "
        "free-text query plus a bounded result count. Each result already carries the "
        "message id, thread id, labels, subject, sender, date, and snippet, so describe "
        "or pick messages from it; call get_email only for a message's full body. "
        "Reading never asks for confirmation."
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
        "plain-text body, labels, and its attachments with part id, file name, type, "
        "and size); refused when the message is outside the mailbox scope the user chose."
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
        "optional cc/bcc, a thread id when replying, and attachments: relative paths of "
        "files in the user's workspace folder, up to 25 MB with the body). The user is "
        "always asked to confirm before it is sent."
    ),
    kind=ToolKind.ACTION,
    approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
    required_role=UserRole.OPERATOR,
    handler=send_email_handler,
    input_model=SendEmailInput,
    output_model=SendEmailOutput,
    precheck=_send_email_precheck,
    module=MODULE_KEY,
    # Runs on the requester's own Gmail connection: only they may decide it (G7).
    requester_only_approval=True,
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
    # Runs on the requester's own Gmail connection: only they may decide it (G7).
    requester_only_approval=True,
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
    # Runs on the requester's own Gmail connection: only they may decide it (G7).
    requester_only_approval=True,
)

SAVE_ATTACHMENT = ToolSpec(
    name="save_attachment",
    description=(
        "Save one attachment of a Gmail message into the user's workspace folder: the "
        "message id, the attachment's part id from get_email, and a relative destination "
        "path (an existing file is never replaced). Only messages inside the mailbox scope "
        "the user chose; up to 70 MB. The user is always asked to confirm."
    ),
    kind=ToolKind.ACTION,
    approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
    required_role=UserRole.OPERATOR,
    handler=save_attachment_handler,
    input_model=SaveAttachmentInput,
    output_model=SaveAttachmentOutput,
    precheck=_save_attachment_precheck,
    module=MODULE_KEY,
    # Writes into the requester's own folder from their own mailbox (G7).
    requester_only_approval=True,
)


def register_google_mail_tools(registry: ToolRegistry) -> None:
    """Register the seven google_mail tools on ``registry``.

    Explicit entry point: importing this module registers nothing. The
    production fold (:func:`~app.modules.google_mail.registry.register_default_google_mail`,
    the reviewed google-mail G3 allowlist entry) calls it once at import
    time on ``default_registry``; tests call it against a LOCAL registry.
    """
    for spec in (
        SEARCH_EMAILS,
        GET_EMAIL,
        LIST_LABELS,
        SEND_EMAIL,
        ARCHIVE_MESSAGE,
        ADD_LABEL,
        SAVE_ATTACHMENT,
    ):
        registry.register(spec)


__all__ = [
    "ADD_LABEL",
    "ARCHIVE_MESSAGE",
    "ATTACHMENTS_TOO_LARGE",
    "ATTACHMENT_NOT_FOUND",
    "ATTACHMENT_TOO_LARGE",
    "EXPIRED",
    "GET_EMAIL",
    "LIST_LABELS",
    "MAX_DOWNLOAD_BYTES",
    "MAX_SEND_BYTES",
    "METADATA_CONCURRENCY",
    "MODULE_KEY",
    "NOT_CONFIGURED",
    "OUT_OF_SCOPE",
    "SAVE_ATTACHMENT",
    "SEARCH_EMAILS",
    "SEND_EMAIL",
    "AddLabelInput",
    "AddLabelOutput",
    "ArchiveMessageInput",
    "ArchiveMessageOutput",
    "EmailAttachment",
    "EmailMessage",
    "EmailSummary",
    "GetEmailInput",
    "GetEmailOutput",
    "ListLabelsInput",
    "ListLabelsOutput",
    "MailLabel",
    "SaveAttachmentInput",
    "SaveAttachmentOutput",
    "SearchEmailsInput",
    "SearchEmailsOutput",
    "SendEmailInput",
    "SendEmailOutput",
    "register_google_mail_tools",
]
