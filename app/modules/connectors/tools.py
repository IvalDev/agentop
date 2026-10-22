"""Read-only folder tools served by the user's local connector (L1).

The server never opens a file: ``list_files`` and ``read_file`` validate a
relative path, ask the online connector for it, and (for reads) turn the
returned bytes into text. ``.txt``/``.md`` decode as UTF-8; ``.pdf``/``.docx``
use :func:`app.modules.documents.extract.extract_text` in a worker thread.
Text is clipped to 50 000 characters and nothing is persisted.

Refusals are :class:`ToolPrecheckError` values so the executor shows the model
a short, user-safe reason (the offline message is the L1 contract text).
"""

import asyncio
import base64
import binascii
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.connectors.hub import (
    REQUEST_TIMEOUT_SECONDS,
    ConnectorHubError,
    ConnectorOfflineError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)
from app.modules.documents.extract import DOCX_CONTENT_TYPE, extract_text
from app.modules.identity.models import UserRole
from app.modules.tools.exceptions import ToolPrecheckError
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolSpec,
)

MAX_PATH_CHARS = 1024
MAX_LIST_ENTRIES = 1000
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
#: Longest base64 payload that can encode MAX_FILE_BYTES.
MAX_BASE64_CHARS = ((MAX_FILE_BYTES + 2) // 3) * 4

ALLOWED_EXTENSIONS = frozenset({".txt", ".md", ".pdf", ".docx"})
_PDF_CONTENT_TYPE = "application/pdf"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

WORKSPACE_OFFLINE_MESSAGE = (
    "El espacio de trabajo no está conectado. Abre el widget y conecta tu carpeta."
)
INVALID_PATH_MESSAGE = (
    "La ruta no es válida. Usa una ruta relativa dentro de la carpeta concedida."
)
UNSUPPORTED_EXTENSION_MESSAGE = "Solo se pueden leer archivos .txt, .md, .pdf o .docx."
FILE_TOO_LARGE_MESSAGE = "El archivo supera el límite de 5 MB."
READ_FAILED_MESSAGE = "No se pudo leer el archivo."
INVALID_RESPONSE_MESSAGE = "El conector devolvió una respuesta no válida."
_FILE_NOT_FOUND_MESSAGE = "El archivo no existe."
_PERMISSION_DENIED_MESSAGE = "No hay permiso para acceder al archivo."

_CONNECTOR_ERROR_MESSAGES = {
    "invalid_path": INVALID_PATH_MESSAGE,
    "not_found": _FILE_NOT_FOUND_MESSAGE,
    "permission_denied": _PERMISSION_DENIED_MESSAGE,
    "too_large": FILE_TOO_LARGE_MESSAGE,
    "io_error": READ_FAILED_MESSAGE,
}


class ListFilesInput(BaseModel):
    """Typed input of ``list_files``; the empty path means the granted root."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        default="",
        max_length=MAX_PATH_CHARS,
        description="Folder path relative to the granted workspace (empty for the root).",
    )


class ListEntry(BaseModel):
    """One entry returned by the connector."""

    name: str
    path: str
    kind: Literal["file", "directory"]
    size: int | None = None
    modified_at: str | None = None


class ListFilesOutput(BaseModel):
    """A folder listing capped at 1000 entries."""

    path: str
    entries: list[ListEntry]


class ReadFileInput(BaseModel):
    """Typed input of ``read_file``: a path relative to the granted root."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=MAX_PATH_CHARS,
        description="File path relative to the granted workspace.",
    )


class ReadFileOutput(BaseModel):
    """Extracted text of one workspace file, clipped for the model."""

    path: str
    text: str
    truncated: bool
    size: int


def _checked_path(path: str) -> str:
    """Return the POSIX-normalized path or refuse traversal/absolute paths."""
    if len(path) > MAX_PATH_CHARS or "\x00" in path:
        raise ToolPrecheckError(INVALID_PATH_MESSAGE)
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_PREFIX.match(normalized):
        raise ToolPrecheckError(INVALID_PATH_MESSAGE)
    if ".." in normalized.split("/"):
        raise ToolPrecheckError(INVALID_PATH_MESSAGE)
    return normalized


def _require_online(context: ToolContext) -> None:
    connectors = context.connectors
    if connectors is None or not connectors.is_online(context.user.id):
        raise ToolPrecheckError(WORKSPACE_OFFLINE_MESSAGE)


def _extension(path: str) -> str:
    return PurePosixPath(_checked_path(path)).suffix.lower()


async def precheck_list_files(context: ToolContext, validated_input: ListFilesInput) -> None:
    """Refuse an offline workspace or a path outside the granted folder."""
    _require_online(context)
    _checked_path(validated_input.path)


async def precheck_read_file(context: ToolContext, validated_input: ReadFileInput) -> None:
    """Refuse an offline workspace, an escaped path, or an unsupported format."""
    _require_online(context)
    extension = _extension(validated_input.path)
    if extension not in ALLOWED_EXTENSIONS:
        raise ToolPrecheckError(UNSUPPORTED_EXTENSION_MESSAGE)


def _precheck_message(exc: ConnectorHubError) -> str:
    if isinstance(exc, (ConnectorOfflineError, ConnectorTimeoutError)):
        return WORKSPACE_OFFLINE_MESSAGE
    if isinstance(exc, ConnectorResponseError):
        return _CONNECTOR_ERROR_MESSAGES.get(exc.code, READ_FAILED_MESSAGE)
    return READ_FAILED_MESSAGE


async def _request(
    context: ToolContext, op: str, args: dict[str, object]
) -> dict[str, object]:
    """Send one operation to the user's connector, translating its failures."""
    connectors = context.connectors
    if connectors is None or not connectors.is_online(context.user.id):
        raise ToolPrecheckError(WORKSPACE_OFFLINE_MESSAGE)
    try:
        result = await connectors.request(
            context.user.id, op, args, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except ConnectorHubError as exc:
        raise ToolPrecheckError(_precheck_message(exc)) from exc
    if not isinstance(result, dict):
        raise ToolPrecheckError(INVALID_RESPONSE_MESSAGE)
    return result


async def list_files(
    context: ToolContext, validated_input: ListFilesInput
) -> ListFilesOutput:
    """List one folder of the user's granted workspace through the connector."""
    result = await _request(context, "list_files", {"path": validated_input.path})
    entries = result.get("entries")
    if not isinstance(entries, list):
        raise ToolPrecheckError(INVALID_RESPONSE_MESSAGE)
    return ListFilesOutput(path=validated_input.path, entries=entries[:MAX_LIST_ENTRIES])


async def read_file(
    context: ToolContext, validated_input: ReadFileInput
) -> ReadFileOutput:
    """Read one allowed file and return its extracted, clipped text."""
    extension = _extension(validated_input.path)
    if extension not in ALLOWED_EXTENSIONS:
        raise ToolPrecheckError(UNSUPPORTED_EXTENSION_MESSAGE)

    result = await _request(context, "read_file", {"path": validated_input.path})
    data_b64 = result.get("data_b64")
    reported_size = result.get("size")
    if (
        not isinstance(data_b64, str)
        or not isinstance(reported_size, int)
        or isinstance(reported_size, bool)
        or reported_size < 0
    ):
        raise ToolPrecheckError(INVALID_RESPONSE_MESSAGE)
    if reported_size > MAX_FILE_BYTES or len(data_b64) > MAX_BASE64_CHARS:
        raise ToolPrecheckError(FILE_TOO_LARGE_MESSAGE)

    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolPrecheckError(READ_FAILED_MESSAGE) from exc
    if len(data) > MAX_FILE_BYTES:
        raise ToolPrecheckError(FILE_TOO_LARGE_MESSAGE)

    if extension in {".txt", ".md"}:
        text = data.decode("utf-8", errors="replace")
    else:
        content_type = _PDF_CONTENT_TYPE if extension == ".pdf" else DOCX_CONTENT_TYPE
        try:
            text = await asyncio.to_thread(extract_text, data, content_type)
        except Exception as exc:
            raise ToolPrecheckError(READ_FAILED_MESSAGE) from exc

    truncated = len(text) > MAX_TEXT_CHARS
    return ReadFileOutput(
        path=validated_input.path,
        text=text[:MAX_TEXT_CHARS],
        truncated=truncated,
        size=len(data),
    )


LIST_FILES = ToolSpec(
    name="list_files",
    description=(
        "List the files and folders of the user's granted local workspace. "
        "Paths are relative to the workspace root."
    ),
    kind=ToolKind.EXTRACT,
    approval_policy=ApprovalPolicy.AUTO,
    required_role=UserRole.VIEWER,
    handler=list_files,
    input_model=ListFilesInput,
    output_model=ListFilesOutput,
    precheck=precheck_list_files,
)

READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read a .txt, .md, .pdf, or .docx file from the user's granted local "
        "workspace. Paths are relative to the workspace root."
    ),
    kind=ToolKind.EXTRACT,
    approval_policy=ApprovalPolicy.AUTO,
    required_role=UserRole.VIEWER,
    handler=read_file,
    input_model=ReadFileInput,
    output_model=ReadFileOutput,
    precheck=precheck_read_file,
)


def register_connector_tools(registry: ToolRegistry) -> None:
    """Register the two connector tools on ``registry`` (once per process)."""
    registry.register(LIST_FILES)
    registry.register(READ_FILE)
