"""Chunked workspace transfers through the local connector (mail-attachments S1-S3).

``app.modules.connectors.transfer`` moves files larger than one WebSocket
message between the server and the user's folder: reads ask ``read_chunk``
slice by slice and refuse a file over the caller's limit or one whose size
changes mid-read; writes upload ``write_chunk`` slices under one random
``upload_id``, ``write_commit`` them, and ``write_abort`` on any failure while
re-raising the original error. Every refusal is a readable
:class:`ToolRefusalError` with the same wording as the workspace tools.
"""

import base64
import re
from collections.abc import Callable

import pytest

from app.modules.connectors.hub import (
    ConnectorOfflineError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)
from app.modules.connectors.tools import (
    ReadFileInput,
    WriteFileInput,
    read_file,
    write_file,
)
from app.modules.connectors.transfer import (
    CHUNK_BYTES,
    read_workspace_file,
    workspace_file_size,
    write_workspace_file,
)
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.tools.exceptions import ToolRefusalError
from app.modules.tools.registry import ToolContext

OFFLINE = "El espacio de trabajo no está conectado. Abre el widget y conecta tu carpeta."
INVALID_PATH = "La ruta no es válida. Usa una ruta relativa dentro de la carpeta concedida."
INVALID_RESPONSE = "El conector devolvió una respuesta no válida."
READ_FAILED = "No se pudo leer el archivo."
FILE_CHANGED = "El archivo cambió mientras se leía. Vuelve a intentarlo."
ALREADY_EXISTS = "Ya existe un elemento con ese nombre en la carpeta concedida."
WRITE_NOT_FOUND = "No existe ningún elemento con esa ruta en la carpeta concedida."
WRITE_PERMISSION_DENIED = "No hay permiso para modificar ese elemento de la carpeta concedida."
WRITE_IO_ERROR = "No se pudo completar la operación en la carpeta concedida."
ATTACHMENT_TOO_LARGE = "El adjunto supera el límite de 25 MB."

USER = CurrentUser(
    id=7,
    tenant_id=3,
    email="viewer@acme.test",
    full_name="Acme Viewer",
    role=UserRole.VIEWER,
    is_superadmin=False,
)
#: Binary content that is not valid UTF-8 and spans three chunks.
BIG = bytes(range(256)) * ((2 * CHUNK_BYTES) // 256) + b"\xff\x00\xfe" * 41

Reply = dict | list | Exception
Respond = Callable[[str, dict], Reply]


class ScriptedRequester:
    """Structural double of ``ConnectorRequester`` answering through ``respond``."""

    def __init__(self, respond: Respond | None = None, *, online: bool = True) -> None:
        self.respond = respond
        self.online = online
        self.calls: list[tuple[int, str, dict]] = []

    def is_online(self, user_id: int) -> bool:
        return self.online

    async def request(self, user_id: int, op: str, args: dict, timeout: float = 30.0) -> Reply:
        self.calls.append((user_id, op, args))
        assert self.respond is not None, f"unexpected connector call {op}"
        reply = self.respond(op, args)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def ops(self) -> list[str]:
        return [op for _, op, _ in self.calls]


def context(requester: ScriptedRequester) -> ToolContext:
    return ToolContext(
        session=None,  # type: ignore[arg-type] - transfers never touch the DB
        tenant_id=USER.tenant_id,
        user=USER,
        connectors=requester,
    )


def encoded(data: bytes, *, size: int | None = None) -> dict:
    return {
        "data_b64": base64.b64encode(data).decode(),
        "size": len(data) if size is None else size,
    }


def queued(*replies: Reply) -> Respond:
    """Answer each call with the next reply, in order."""
    pending = list(replies)
    return lambda op, args: pending.pop(0)


def serving(data: bytes) -> Respond:
    """A desktop answering ``read_chunk`` for a file holding ``data``."""

    def respond(op: str, args: dict) -> Reply:
        assert op == "read_chunk"
        start = args["offset"]
        return encoded(data[start : start + args["length"]], size=len(data))

    return respond


def uploads(failures: dict[tuple[str, int], Exception | dict] | None = None) -> Respond:
    """A desktop accepting uploads; ``failures`` replaces the n-th reply of an op."""
    failures = failures or {}
    seen: dict[str, int] = {}

    def respond(op: str, args: dict) -> Reply:
        index = seen.get(op, 0)
        seen[op] = index + 1
        if (op, index) in failures:
            return failures[(op, index)]
        if op == "write_chunk":
            written = args["offset"] + len(base64.b64decode(args["data_b64"]))
            return {"path": args["path"], "upload_id": args["upload_id"], "written": written}
        if op == "write_commit":
            return {"path": args["path"], "bytes": args["size"], "created": True}
        assert op == "write_abort"
        return {"path": args["path"], "removed": True}

    return respond


def chunk_call(upload_id: str, offset: int, data: bytes) -> tuple[int, str, dict]:
    args = {
        "path": "out/file.bin",
        "upload_id": upload_id,
        "offset": offset,
        "data_b64": base64.b64encode(data).decode(),
    }
    return (USER.id, "write_chunk", args)


def upload_id_of(requester: ScriptedRequester) -> str:
    return requester.calls[0][2]["upload_id"]


# --- workspace_file_size ---


async def test_workspace_file_size_asks_file_info_and_returns_the_size() -> None:
    requester = ScriptedRequester(queued({"path": "docs/a.pdf", "size": 1234}))

    size = await workspace_file_size(context(requester), "docs/a.pdf")

    assert size == 1234
    assert requester.calls == [(USER.id, "file_info", {"path": "docs/a.pdf"})]


@pytest.mark.parametrize(
    "reply",
    [{"path": "a.pdf"}, {"size": "12"}, {"size": True}, {"size": -1}, {"size": 1.5}, ["x"]],
)
async def test_workspace_file_size_refuses_a_malformed_reply(reply: Reply) -> None:
    requester = ScriptedRequester(queued(reply))

    with pytest.raises(ToolRefusalError) as exc:
        await workspace_file_size(context(requester), "a.pdf")

    assert exc.value.reason == INVALID_RESPONSE


# --- read_workspace_file ---


async def test_read_reassembles_a_multi_chunk_binary_file_up_to_max_bytes() -> None:
    requester = ScriptedRequester(serving(BIG))

    data = await read_workspace_file(
        context(requester), "in/big.bin", max_bytes=len(BIG), too_large_message="x"
    )

    assert data == BIG
    assert requester.calls == [
        (USER.id, "read_chunk", {"path": "in/big.bin", "offset": offset, "length": CHUNK_BYTES})
        for offset in (0, CHUNK_BYTES, 2 * CHUNK_BYTES)
    ]


async def test_read_of_an_empty_file_asks_a_single_chunk() -> None:
    requester = ScriptedRequester(serving(b""))

    data = await read_workspace_file(
        context(requester), "empty.txt", max_bytes=10, too_large_message="x"
    )

    assert data == b""
    assert requester.calls == [
        (USER.id, "read_chunk", {"path": "empty.txt", "offset": 0, "length": CHUNK_BYTES})
    ]


async def test_read_refuses_a_file_over_max_bytes_before_reading_further() -> None:
    requester = ScriptedRequester(serving(BIG))

    with pytest.raises(ToolRefusalError) as exc:
        await read_workspace_file(
            context(requester),
            "in/big.bin",
            max_bytes=len(BIG) - 1,
            too_large_message=ATTACHMENT_TOO_LARGE,
        )

    assert exc.value.reason == ATTACHMENT_TOO_LARGE
    assert requester.ops() == ["read_chunk"]


async def test_read_refuses_a_file_whose_size_changes_between_chunks() -> None:
    first = encoded(BIG[:CHUNK_BYTES], size=len(BIG))
    second = encoded(BIG[CHUNK_BYTES : 2 * CHUNK_BYTES], size=len(BIG) + 1)
    requester = ScriptedRequester(queued(first, second))

    with pytest.raises(ToolRefusalError) as exc:
        await read_workspace_file(
            context(requester), "in/big.bin", max_bytes=10 * CHUNK_BYTES, too_large_message="x"
        )

    assert exc.value.reason == FILE_CHANGED
    assert requester.ops() == ["read_chunk", "read_chunk"]


@pytest.mark.parametrize(
    "reply",
    [
        {"data_b64": 1, "size": 3},
        {"data_b64": "YWJj"},
        {"data_b64": "YWJj", "size": True},
        {"data_b64": "YWJj", "size": -1},
        ["YWJj"],
        # Fewer bytes than the reported size (a retry here would never end).
        encoded(b"ab", size=3),
        # More bytes than the reported size.
        encoded(b"abc", size=2),
        # A payload no chunk can be, refused before decoding it.
        {"data_b64": "A" * ((CHUNK_BYTES // 3 + 2) * 4), "size": 3 * CHUNK_BYTES},
    ],
)
async def test_read_refuses_a_malformed_chunk_reply(reply: Reply) -> None:
    requester = ScriptedRequester(queued(reply))

    with pytest.raises(ToolRefusalError) as exc:
        await read_workspace_file(
            context(requester), "a.bin", max_bytes=10 * CHUNK_BYTES, too_large_message="x"
        )

    assert exc.value.reason == INVALID_RESPONSE


async def test_read_refuses_corrupt_base64_like_read_file() -> None:
    requester = ScriptedRequester(queued({"data_b64": "%%%", "size": 3}))

    with pytest.raises(ToolRefusalError) as exc:
        await read_workspace_file(context(requester), "a.bin", max_bytes=10, too_large_message="x")

    assert exc.value.reason == READ_FAILED


@pytest.mark.parametrize(
    ("requester", "calls"),
    [
        (ScriptedRequester(online=False), 0),
        (ScriptedRequester(queued(ConnectorOfflineError())), 1),
        (ScriptedRequester(queued(ConnectorTimeoutError())), 1),
    ],
)
async def test_read_refuses_an_offline_workspace(requester: ScriptedRequester, calls: int) -> None:
    with pytest.raises(ToolRefusalError) as exc:
        await read_workspace_file(context(requester), "a.bin", max_bytes=10, too_large_message="x")

    assert exc.value.reason == OFFLINE
    assert len(requester.calls) == calls


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("invalid_path", INVALID_PATH),
        ("not_found", "El archivo no existe."),
        ("permission_denied", "No hay permiso para acceder al archivo."),
        ("io_error", READ_FAILED),
        ("unexpected", READ_FAILED),
    ],
)
async def test_read_ops_map_connector_errors_with_the_read_messages(
    code: str, message: str
) -> None:
    for call in (
        lambda ctx: workspace_file_size(ctx, "a.bin"),
        lambda ctx: read_workspace_file(ctx, "a.bin", max_bytes=10, too_large_message="x"),
    ):
        requester = ScriptedRequester(queued(ConnectorResponseError(code, "detail")))

        with pytest.raises(ToolRefusalError) as exc:
            await call(context(requester))

        assert exc.value.reason == message


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: workspace_file_size(ctx, "../secret.txt"),
        lambda ctx: read_workspace_file(ctx, "/etc/passwd", max_bytes=10, too_large_message="x"),
        lambda ctx: write_workspace_file(ctx, "C:\\Windows\\x.bin", b"data"),
        lambda ctx: write_workspace_file(ctx, ".", b"data"),
    ],
)
async def test_transfers_refuse_an_escaped_path_before_any_request(call) -> None:
    requester = ScriptedRequester(uploads())

    with pytest.raises(ToolRefusalError) as exc:
        await call(context(requester))

    assert exc.value.reason == INVALID_PATH
    assert requester.calls == []


# --- write_workspace_file ---


async def test_write_uploads_ordered_chunks_then_commits() -> None:
    requester = ScriptedRequester(uploads())

    result = await write_workspace_file(context(requester), "out/file.bin", BIG)

    upload_id = upload_id_of(requester)
    assert result == {"path": "out/file.bin", "bytes": len(BIG), "created": True}
    assert requester.calls == [
        chunk_call(upload_id, 0, BIG[:CHUNK_BYTES]),
        chunk_call(upload_id, CHUNK_BYTES, BIG[CHUNK_BYTES : 2 * CHUNK_BYTES]),
        chunk_call(upload_id, 2 * CHUNK_BYTES, BIG[2 * CHUNK_BYTES :]),
        (
            USER.id,
            "write_commit",
            {"path": "out/file.bin", "upload_id": upload_id, "size": len(BIG)},
        ),
    ]


async def test_write_of_empty_data_sends_one_empty_chunk() -> None:
    requester = ScriptedRequester(uploads())

    result = await write_workspace_file(context(requester), "out/file.bin", b"")

    upload_id = upload_id_of(requester)
    assert result == {"path": "out/file.bin", "bytes": 0, "created": True}
    assert requester.calls == [
        chunk_call(upload_id, 0, b""),
        (USER.id, "write_commit", {"path": "out/file.bin", "upload_id": upload_id, "size": 0}),
    ]


async def test_write_uses_one_fresh_32_hex_upload_id_per_upload() -> None:
    first, second = ScriptedRequester(uploads()), ScriptedRequester(uploads())

    await write_workspace_file(context(first), "out/file.bin", BIG)
    await write_workspace_file(context(second), "out/file.bin", b"x")

    upload_id = upload_id_of(first)
    assert re.fullmatch(r"[0-9a-f]{32}", upload_id)
    assert {args["upload_id"] for _, _, args in first.calls} == {upload_id}
    assert upload_id_of(second) != upload_id


@pytest.mark.parametrize(
    ("failure", "message", "ops"),
    [
        (
            ("write_chunk", 1),
            ConnectorResponseError("io_error", "disk full"),
            ["write_chunk", "write_chunk", "write_abort"],
        ),
        (
            ("write_commit", 0),
            ConnectorResponseError("already_exists", "exists"),
            ["write_chunk"] * 3 + ["write_commit", "write_abort"],
        ),
    ],
)
async def test_a_failed_upload_is_aborted_and_reraises_the_original_error(
    failure: tuple[str, int], message: ConnectorResponseError, ops: list[str]
) -> None:
    requester = ScriptedRequester(uploads({failure: message}))

    with pytest.raises(ToolRefusalError) as exc:
        await write_workspace_file(context(requester), "out/file.bin", BIG)

    expected = WRITE_IO_ERROR if message.code == "io_error" else ALREADY_EXISTS
    assert exc.value.reason == expected
    assert requester.ops() == ops
    assert requester.calls[-1] == (
        USER.id,
        "write_abort",
        {"path": "out/file.bin", "upload_id": upload_id_of(requester)},
    )


@pytest.mark.parametrize("abort_error", [ConnectorTimeoutError(), RuntimeError("boom")])
async def test_a_failing_abort_does_not_mask_the_original_error(abort_error: Exception) -> None:
    requester = ScriptedRequester(
        uploads(
            {
                ("write_chunk", 0): ConnectorResponseError("permission_denied", "denied"),
                ("write_abort", 0): abort_error,
            }
        )
    )

    with pytest.raises(ToolRefusalError) as exc:
        await write_workspace_file(context(requester), "out/file.bin", b"data")

    assert exc.value.reason == WRITE_PERMISSION_DENIED
    assert requester.ops() == ["write_chunk", "write_abort"]


@pytest.mark.parametrize(
    "failure",
    [
        (("write_chunk", 0), {"written": "4"}),
        (("write_chunk", 0), {"written": True}),
        (("write_chunk", 0), {"written": 3}),
        (("write_commit", 0), {"bytes": 4, "created": "yes"}),
        (("write_commit", 0), {"bytes": 5, "created": True}),
        (("write_commit", 0), ["done"]),
    ],
)
async def test_a_malformed_upload_reply_is_refused_and_aborted(
    failure: tuple[tuple[str, int], Reply],
) -> None:
    requester = ScriptedRequester(uploads(dict([failure])))

    with pytest.raises(ToolRefusalError) as exc:
        await write_workspace_file(context(requester), "out/file.bin", b"data")

    assert exc.value.reason == INVALID_RESPONSE
    assert requester.ops()[-1] == "write_abort"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("not_found", WRITE_NOT_FOUND),
        ("permission_denied", WRITE_PERMISSION_DENIED),
        ("io_error", WRITE_IO_ERROR),
        ("already_exists", ALREADY_EXISTS),
        ("invalid_path", INVALID_PATH),
    ],
)
async def test_write_ops_map_connector_errors_with_the_write_messages(
    code: str, message: str
) -> None:
    requester = ScriptedRequester(uploads({("write_chunk", 0): ConnectorResponseError(code, "x")}))

    with pytest.raises(ToolRefusalError) as exc:
        await write_workspace_file(context(requester), "out/file.bin", b"data")

    assert exc.value.reason == message


async def test_write_refuses_an_offline_workspace_without_any_request() -> None:
    requester = ScriptedRequester(online=False)

    with pytest.raises(ToolRefusalError) as exc:
        await write_workspace_file(context(requester), "out/file.bin", b"data")

    assert exc.value.reason == OFFLINE
    assert requester.calls == []


# --- the workspace tools that share the connector request and path checks ---


async def test_existing_workspace_tools_keep_their_requests_and_messages() -> None:
    reader = ScriptedRequester(queued(encoded(b"hola")))
    output = await read_file(context(reader), ReadFileInput(path="notes.txt"))

    assert (output.text, output.size, output.truncated) == ("hola", 4, False)
    assert reader.calls == [(USER.id, "read_file", {"path": "notes.txt"})]

    writer = ScriptedRequester(queued(ConnectorResponseError("not_found", "missing")))
    with pytest.raises(ToolRefusalError) as exc:
        await write_file(context(writer), WriteFileInput(path="a/b.txt", content="hola"))
    assert exc.value.reason == WRITE_NOT_FOUND

    with pytest.raises(ToolRefusalError) as exc:
        await read_file(context(ScriptedRequester(online=False)), ReadFileInput(path="n.txt"))
    assert exc.value.reason == OFFLINE
