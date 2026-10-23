"""Chunked binary transfers between the server and the user's local folder.

A WebSocket message to the connector is capped at 16 MiB on both ends, so
files that may be larger (mail attachments, up to Gmail's limits) travel in
``CHUNK_BYTES`` slices of raw bytes, base64-encoded inside the existing
``request``/``response`` frames:

- :func:`workspace_file_size` asks ``file_info`` for a file's size.
- :func:`read_workspace_file` asks ``read_chunk`` slice by slice, refuses a
  file over the caller's ``max_bytes`` after its first slice, and refuses one
  whose reported size changes between slices.
- :func:`write_workspace_file` uploads ``write_chunk`` slices under one random
  ``upload_id`` (at least one, so empty files exist too) and ``write_commit``
  renames the connector's temporary file onto the destination, which is never
  overwritten. Any failure sends a best-effort ``write_abort`` and re-raises
  the original error, so the reason the human sees is the one that happened.

Paths are checked with the workspace tools' rules before any request, and the
connector's answers are refused with their wording (reads with the read
messages, uploads with the write ones) as :class:`ToolRefusalError` values,
because these functions run inside tool handlers. Nothing is persisted.
"""

import base64
import binascii
import logging
from collections.abc import Callable
from uuid import uuid4

from app.modules.connectors.tools import (
    INVALID_RESPONSE_MESSAGE,
    READ_FAILED_MESSAGE,
    checked_path,
    checked_target,
    request_connector,
)
from app.modules.tools.exceptions import ToolPrecheckError, ToolRefusalError
from app.modules.tools.registry import ToolContext

logger = logging.getLogger(__name__)

CHUNK_BYTES = 4 * 1024 * 1024
#: Longest base64 payload that can encode one chunk.
MAX_CHUNK_BASE64_CHARS = ((CHUNK_BYTES + 2) // 3) * 4

FILE_CHANGED_MESSAGE = "El archivo cambió mientras se leía. Vuelve a intentarlo."


def _is_size(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _refusing_path(check: Callable[[str], str], path: str) -> None:
    """Apply a workspace path check, refusing as a handler does."""
    try:
        check(path)
    except ToolPrecheckError as exc:
        raise ToolRefusalError(exc.reason) from exc


async def workspace_file_size(context: ToolContext, path: str) -> int:
    """Return the byte size of one workspace file (connector op ``file_info``)."""
    _refusing_path(checked_path, path)
    result = await request_connector(context, "file_info", {"path": path})
    size = result.get("size")
    if not _is_size(size):
        raise ToolRefusalError(INVALID_RESPONSE_MESSAGE)
    return size


def _chunk_reply(result: dict[str, object]) -> tuple[str, int]:
    """Validate the shape of one ``read_chunk`` answer: its payload and total size."""
    data_b64, size = result.get("data_b64"), result.get("size")
    if not isinstance(data_b64, str) or not _is_size(size):
        raise ToolRefusalError(INVALID_RESPONSE_MESSAGE)
    if len(data_b64) > MAX_CHUNK_BASE64_CHARS:
        raise ToolRefusalError(INVALID_RESPONSE_MESSAGE)
    return data_b64, size


def _decoded(data_b64: str) -> bytes:
    try:
        return base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolRefusalError(READ_FAILED_MESSAGE) from exc


async def read_workspace_file(
    context: ToolContext, path: str, *, max_bytes: int, too_large_message: str
) -> bytes:
    """Read one workspace file in chunks, refusing it over ``max_bytes``.

    The first chunk's ``size`` fixes the file's length: over ``max_bytes`` it
    is refused with ``too_large_message`` before another chunk is asked, and a
    later chunk reporting another size means the file changed mid-read.
    """
    _refusing_path(checked_path, path)
    data = bytearray()
    size: int | None = None
    while True:
        offset = len(data)
        result = await request_connector(
            context, "read_chunk", {"path": path, "offset": offset, "length": CHUNK_BYTES}
        )
        data_b64, reported = _chunk_reply(result)
        if size is not None and reported != size:
            raise ToolRefusalError(FILE_CHANGED_MESSAGE)
        if reported > max_bytes:
            raise ToolRefusalError(too_large_message)
        size = reported
        chunk = _decoded(data_b64)
        # Anything but the exact slice is a broken answer (and would never end).
        if len(chunk) != min(CHUNK_BYTES, size - offset):
            raise ToolRefusalError(INVALID_RESPONSE_MESSAGE)
        data += chunk
        if len(data) >= size:
            return bytes(data)


async def _abort_upload(context: ToolContext, path: str, upload_id: str) -> None:
    """Ask the connector to drop an unfinished upload; never raise."""
    try:
        await request_connector(context, "write_abort", {"path": path, "upload_id": upload_id})
    except Exception:
        logger.warning("workspace upload abort failed", exc_info=True)


async def _upload(context: ToolContext, path: str, upload_id: str, data: bytes) -> dict:
    """Send ``data`` as ordered ``write_chunk`` slices, then ``write_commit`` them."""
    view = memoryview(data)
    offset = 0
    while True:
        chunk = view[offset : offset + CHUNK_BYTES]
        result = await request_connector(
            context,
            "write_chunk",
            {
                "path": path,
                "upload_id": upload_id,
                "offset": offset,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
            },
        )
        offset += len(chunk)
        written = result.get("written")
        if not _is_size(written) or written != offset:
            raise ToolRefusalError(INVALID_RESPONSE_MESSAGE)
        if offset >= len(data):
            break

    result = await request_connector(
        context, "write_commit", {"path": path, "upload_id": upload_id, "size": len(data)}
    )
    written, created = result.get("bytes"), result.get("created")
    if not _is_size(written) or written != len(data) or not isinstance(created, bool):
        raise ToolRefusalError(INVALID_RESPONSE_MESSAGE)
    return result


async def write_workspace_file(context: ToolContext, path: str, data: bytes) -> dict:
    """Create one workspace file from ``data`` in chunks and return the commit result.

    The connector refuses an existing destination (``already_exists``); on any
    failure the upload is aborted best-effort and the original error re-raised.
    """
    _refusing_path(checked_target, path)
    upload_id = uuid4().hex
    try:
        return await _upload(context, path, upload_id, data)
    except Exception:
        await _abort_upload(context, path, upload_id)
        raise
