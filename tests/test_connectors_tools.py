"""Folder tools served through the local connector: handlers and path rules.

Contract L1 (agent-workspace-local S5): ``list_files`` and ``read_file`` are
read-only viewer tools that ask the user's online connector. The server never
touches the filesystem: it validates the relative path, forwards the operation,
and turns the connector's bytes into text (``.txt``/``.md`` inline, ``.pdf``
and ``.docx`` through the documents extractor in a thread). Nothing is
persisted.
"""

import base64
from io import BytesIO

import pytest
from docx import Document as DocxDocument
from pydantic import BaseModel

from app.core.db import Database
from app.modules.connectors import tools as connector_tools
from app.modules.connectors.hub import ConnectorResponseError
from app.modules.connectors.tools import (
    ListFilesInput,
    ReadFileInput,
    list_files,
    precheck_list_files,
    precheck_read_file,
    read_file,
    register_connector_tools,
)
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.tools import builtin
from app.modules.tools.exceptions import ToolPrecheckError
from app.modules.tools.executor import execute_tool
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolSpec,
    default_registry,
)

OFFLINE = "El espacio de trabajo no está conectado. Abre el widget y conecta tu carpeta."
INVALID_PATH = "La ruta no es válida. Usa una ruta relativa dentro de la carpeta concedida."
UNSUPPORTED_EXTENSION = "Solo se pueden leer archivos .txt, .md, .pdf o .docx."
TOO_LARGE = "El archivo supera el límite de 5 MB."
READ_FAILED = "No se pudo leer el archivo."
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
MAX_LIST_ENTRIES = 1_000


class FakeRequester:
    """Structural double of ``ConnectorRequester`` with a scripted response."""

    def __init__(
        self,
        *,
        online: bool = True,
        result: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        self.online = online
        self.result = {} if result is None else result
        self.error = error
        self.calls: list[tuple[int, str, dict, float]] = []

    def is_online(self, user_id: int) -> bool:
        return self.online

    async def request(
        self, user_id: int, op: str, args: dict, timeout: float = 30.0
    ) -> dict:
        self.calls.append((user_id, op, args, timeout))
        if self.error is not None:
            raise self.error
        return self.result


def context(connectors, user: CurrentUser) -> ToolContext:
    return ToolContext(
        session=None,  # type: ignore[arg-type] - these handlers never touch the DB
        tenant_id=user.tenant_id,
        user=user,
        connectors=connectors,
    )


def encoded(data: bytes) -> dict:
    return {"data_b64": base64.b64encode(data).decode(), "size": len(data)}


def test_connector_tools_are_viewer_auto_extract_core_tools() -> None:
    """Both tools are read-only, approval-free, and available to every engine."""
    registry = ToolRegistry()
    register_connector_tools(registry)

    for name, input_model, output_model in (
        ("list_files", ListFilesInput, connector_tools.ListFilesOutput),
        ("read_file", ReadFileInput, connector_tools.ReadFileOutput),
    ):
        spec = registry.get(name)
        assert spec.kind is ToolKind.EXTRACT
        assert spec.approval_policy is ApprovalPolicy.AUTO
        assert spec.required_role is UserRole.VIEWER
        assert spec.module is None
        assert spec.input_model is input_model
        assert spec.output_model is output_model


def test_existing_search_documents_tool_is_unchanged() -> None:
    """Registering the connector tools leaves the production search tool intact."""
    spec = default_registry.get("search_documents")

    assert spec is builtin.SEARCH_DOCUMENTS
    assert spec.approval_policy is ApprovalPolicy.AUTO
    assert spec.required_role is UserRole.VIEWER


async def test_both_tools_refuse_an_offline_workspace_with_a_readable_message(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """Offline is a precheck refusal, before any connector request."""
    user = seeded_users["viewer@acme.test"]
    requester = FakeRequester(online=False)

    for precheck, payload in (
        (precheck_list_files, ListFilesInput()),
        (precheck_read_file, ReadFileInput(path="notes.txt")),
    ):
        with pytest.raises(ToolPrecheckError) as exc:
            await precheck(context(requester, user), payload)
        assert exc.value.reason == OFFLINE
    assert requester.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "../secret.txt",
        "notes/../../secret.txt",
        "notes/..",
        "bad\x00name.txt",
    ],
)
async def test_paths_outside_the_workspace_are_rejected(
    seeded_users: dict[str, CurrentUser], path: str
) -> None:
    """Absolute paths, parent traversal, NUL bytes, and over-long paths all fail."""
    user = seeded_users["viewer@acme.test"]

    with pytest.raises(ToolPrecheckError) as exc:
        await precheck_list_files(context(FakeRequester(), user), ListFilesInput(path=path))

    assert exc.value.reason == INVALID_PATH


async def test_an_invalid_path_never_reaches_the_connector(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """The executor surfaces the precheck refusal and the handler is not called."""
    registry = ToolRegistry()
    register_connector_tools(registry)
    requester = FakeRequester(result={"entries": []})
    user = seeded_users["viewer@acme.test"]

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            registry, session, user, "list_files", {"path": "../secret"}, connectors=requester
        )

    assert result.status == "error"
    assert result.error_kind == "precheck"
    assert result.error == f"precheck_failed: {INVALID_PATH}"
    assert requester.calls == []


async def test_a_path_longer_than_1024_characters_is_rejected_at_validation(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """The bounded input model stops an over-long path before precheck or handler."""
    registry = ToolRegistry()
    register_connector_tools(registry)
    requester = FakeRequester(result={"entries": []})
    user = seeded_users["viewer@acme.test"]

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            registry, session, user, "list_files", {"path": "x" * 1025}, connectors=requester
        )

    assert result.status == "error"
    assert result.error_kind == "validation"
    assert "1024" in (result.error or "")
    assert requester.calls == []


async def test_an_offline_workspace_is_refused_by_the_executor(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """Through the public tool interface, offline still carries the readable reason."""
    registry = ToolRegistry()
    register_connector_tools(registry)
    user = seeded_users["viewer@acme.test"]

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            registry,
            session,
            user,
            "list_files",
            {},
            connectors=FakeRequester(online=False),
        )

    assert result.status == "error"
    assert result.error_kind == "precheck"
    assert result.error == f"precheck_failed: {OFFLINE}"


async def test_the_executor_puts_the_connector_hub_in_the_tool_context(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """Handlers receive the hub the caller passed to ``execute_tool``."""
    requester = FakeRequester()
    captured: list[object] = []

    async def handler(context: ToolContext, validated_input: BaseModel) -> dict:
        captured.append(context.connectors)
        return {"seen": True}

    class NoInput(BaseModel):
        pass

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="peek_hub",
            description="test-only handler that records its connector hub",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.VIEWER,
            handler=handler,
            input_model=NoInput,
        )
    )
    user = seeded_users["viewer@acme.test"]

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            registry, session, user, "peek_hub", {}, connectors=requester
        )

    assert result.status == "ok"
    assert captured == [requester]


async def test_list_files_returns_the_connector_entries_capped_at_a_thousand(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """The server echoes the folder path and never returns more than 1000 entries."""
    user = seeded_users["viewer@acme.test"]
    entries = [
        {
            "name": f"file-{index}.txt",
            "path": f"docs/file-{index}.txt",
            "kind": "file",
            "size": index,
            "modified_at": "2026-10-20T10:00:00Z",
        }
        for index in range(MAX_LIST_ENTRIES + 1)
    ]
    requester = FakeRequester(result={"path": "docs", "entries": entries})

    output = await list_files(context(requester, user), ListFilesInput(path="docs"))

    assert output.path == "docs"
    assert len(output.entries) == MAX_LIST_ENTRIES
    assert output.entries[0].name == "file-0.txt"
    assert output.entries[0].kind == "file"
    assert requester.calls == [(user.id, "list_files", {"path": "docs"}, 30.0)]


async def test_list_files_handler_fails_readably_when_the_device_went_offline(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """A disconnect between precheck and handler is still the offline message."""
    user = seeded_users["viewer@acme.test"]

    with pytest.raises(ToolPrecheckError) as exc:
        await list_files(
            context(FakeRequester(online=False), user), ListFilesInput()
        )

    assert exc.value.reason == OFFLINE


@pytest.mark.parametrize(("name", "text"), [("notes.txt", "hola"), ("readme.md", "# Título")])
async def test_read_file_decodes_plain_text(
    seeded_users: dict[str, CurrentUser], name: str, text: str
) -> None:
    """``.txt``/``.md`` are decoded as UTF-8 and reported with their size."""
    user = seeded_users["viewer@acme.test"]
    data = text.encode()
    requester = FakeRequester(result=encoded(data))

    output = await read_file(context(requester, user), ReadFileInput(path=name))

    assert (output.path, output.text, output.truncated, output.size) == (
        name,
        text,
        False,
        len(data),
    )
    assert requester.calls == [(user.id, "read_file", {"path": name}, 30.0)]


async def test_read_file_extracts_docx_text(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """``.docx`` goes through the documents extractor, not a raw UTF-8 decode."""
    user = seeded_users["viewer@acme.test"]
    document = DocxDocument()
    document.add_paragraph("Hola desde Word")
    buffer = BytesIO()
    document.save(buffer)

    output = await read_file(
        context(FakeRequester(result=encoded(buffer.getvalue())), user),
        ReadFileInput(path="informe.docx"),
    )

    assert "Hola desde Word" in output.text
    assert output.truncated is False


async def test_read_file_extracts_pdf_text_in_a_thread(
    seeded_users: dict[str, CurrentUser], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.pdf`` uses the extraction helper with the PDF MIME type off the event loop."""
    user = seeded_users["viewer@acme.test"]
    calls: list[tuple[bytes, str]] = []

    def fake_extract(data: bytes, content_type: str) -> str:
        calls.append((data, content_type))
        return "texto del PDF"

    monkeypatch.setattr(connector_tools, "extract_text", fake_extract)

    output = await read_file(
        context(FakeRequester(result=encoded(b"%PDF-fake")), user),
        ReadFileInput(path="contrato.pdf"),
    )

    assert output.text == "texto del PDF"
    assert calls == [(b"%PDF-fake", "application/pdf")]


@pytest.mark.parametrize("path", ["notes.exe", "image.png", "archive.zip", "no-extension"])
async def test_read_file_rejects_unsupported_extensions(
    seeded_users: dict[str, CurrentUser], path: str
) -> None:
    """Only the four L1 formats are readable."""
    user = seeded_users["viewer@acme.test"]

    with pytest.raises(ToolPrecheckError) as exc:
        await precheck_read_file(context(FakeRequester(), user), ReadFileInput(path=path))

    assert exc.value.reason == UNSUPPORTED_EXTENSION


async def test_read_file_rejects_files_over_five_megabytes(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """The connector-reported size is checked before decoding anything."""
    user = seeded_users["viewer@acme.test"]
    requester = FakeRequester(result={"data_b64": "", "size": MAX_FILE_BYTES + 1})

    with pytest.raises(ToolPrecheckError) as exc:
        await read_file(context(requester, user), ReadFileInput(path="big.txt"))

    assert exc.value.reason == TOO_LARGE


async def test_read_file_truncates_text_at_fifty_thousand_characters(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """Long files travel to the model clipped, with ``truncated`` set."""
    user = seeded_users["viewer@acme.test"]
    text = "a" * (MAX_TEXT_CHARS + 5)

    output = await read_file(
        context(FakeRequester(result=encoded(text.encode())), user),
        ReadFileInput(path="long.txt"),
    )

    assert len(output.text) == MAX_TEXT_CHARS
    assert output.text == text[:MAX_TEXT_CHARS]
    assert output.truncated is True
    assert output.size == len(text)


async def test_read_file_maps_connector_errors_to_readable_reasons(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """A ``too_large`` response and corrupt base64 both fail readably, not as 500s."""
    user = seeded_users["viewer@acme.test"]

    with pytest.raises(ToolPrecheckError) as exc:
        await read_file(
            context(FakeRequester(error=ConnectorResponseError("too_large", ">5MB")), user),
            ReadFileInput(path="big.txt"),
        )
    assert exc.value.reason == TOO_LARGE

    with pytest.raises(ToolPrecheckError) as exc:
        await read_file(
            context(FakeRequester(result={"data_b64": "%%%", "size": 3}), user),
            ReadFileInput(path="notes.txt"),
        )
    assert exc.value.reason == READ_FAILED


def test_connector_tool_inputs_forbid_extra_fields() -> None:
    """The model can never smuggle tenant ids or operations into these tools."""
    for model in (ListFilesInput, ReadFileInput):
        assert issubclass(model, BaseModel)
        assert model.model_config.get("extra") == "forbid"
        assert set(model.model_fields) == {"path"}
