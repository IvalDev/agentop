"""Folder tools served through the local connector: handlers and path rules.

Contract L1 (agent-workspace-local S5): ``list_files`` and ``read_file`` are
read-only viewer tools that ask the user's online connector. The server never
touches the filesystem: it validates the relative path, forwards the operation,
and turns the connector's bytes into text (``.txt``/``.md`` inline, ``.pdf``
and ``.docx`` through the documents extractor in a thread). Nothing is
persisted.

Contract L2 (S6): ``write_file``, ``create_folder``, ``move_path``, and
``delete_path`` are approval-gated ACTION tools: their precheck refuses an
offline workspace, an escaped path, or oversized content *before* any approval
request exists, and the approved input is exactly what reaches the connector.
"""

import base64
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID

import pytest
from docx import Document as DocxDocument
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.db import Database
from app.modules.approvals.exceptions import (
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalStateError,
)
from app.modules.approvals.models import ApprovalStatus
from app.modules.approvals.service import (
    approve_request,
    create_approval_request,
    execute_approved_action,
    get_approval_request,
    reject_request,
)
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
ALREADY_EXISTS = "Ya existe un elemento con ese nombre en la carpeta concedida."
CONTENT_TOO_LARGE = "El contenido supera el límite de 50 000 caracteres."
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
MAX_LIST_ENTRIES = 1_000

#: Inputs of the four L2 write tools, as the model would send them.
WRITE_CALLS = {
    "write_file": {"path": "notes.txt", "content": "hola"},
    "create_folder": {"path": "docs"},
    "move_path": {"from_path": "a.txt", "to_path": "b.txt"},
    "delete_path": {"path": "a.txt"},
}

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
TTL_SECONDS = 3600
APPROVAL_SETTINGS = Settings(approval_ttl_seconds=TTL_SECONDS)


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

    async def request(self, user_id: int, op: str, args: dict, timeout: float = 30.0) -> dict:
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
        result = await execute_tool(registry, session, user, "peek_hub", {}, connectors=requester)

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
        await list_files(context(FakeRequester(online=False), user), ListFilesInput())

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


# --- L2 writes (S6): declaration, prechecks, handlers, and the approval gate ---


def write_registry() -> ToolRegistry:
    """A local registry carrying the production connector reads and writes."""
    registry = ToolRegistry()
    register_connector_tools(registry)
    return registry


async def _request_approval(
    db: Database, registry: ToolRegistry, user: CurrentUser, tool_name: str, raw_input: dict
) -> UUID:
    """Persist the pending approval a chat turn would leave behind."""
    async with db.sessionmaker() as session:
        request = await create_approval_request(
            session,
            registry,
            user,
            tool_name,
            raw_input,
            settings=APPROVAL_SETTINGS,
            now=T0,
        )
        await session.commit()
        return request.id


async def _approve(
    db: Database, registry: ToolRegistry, user: CurrentUser, request_id: UUID
) -> None:
    async with db.sessionmaker() as session:
        await approve_request(session, user, request_id, now=T0, registry=registry)


async def _execute_approved(
    db: Database,
    registry: ToolRegistry,
    user: CurrentUser,
    request_id: UUID,
    requester,
    now: datetime = T0,
):
    async with db.sessionmaker() as session:
        return await execute_approved_action(
            session, registry, user, request_id, now=now, connectors=requester
        )


@pytest.mark.parametrize("name", sorted(WRITE_CALLS))
def test_write_tools_are_viewer_actions_that_always_need_approval(name: str) -> None:
    """The four writes are ACTION + REQUIRE_APPROVAL, declared once each."""
    spec = write_registry().get(name)

    assert spec.kind is ToolKind.ACTION
    assert spec.approval_policy is ApprovalPolicy.REQUIRE_APPROVAL
    assert spec.required_role is UserRole.VIEWER
    assert spec.requester_only_approval is True
    assert spec.module is None
    assert spec.precheck is not None
    assert spec.output_model is not None


def test_write_tool_inputs_are_closed_and_named_per_contract() -> None:
    """Each write takes exactly the fields of the L2 contract and nothing else."""
    registry = write_registry()
    expected = {
        "write_file": {"path", "content"},
        "create_folder": {"path"},
        "move_path": {"from_path", "to_path"},
        "delete_path": {"path"},
    }

    for name, fields in expected.items():
        model = registry.get(name).input_model
        assert model.model_config.get("extra") == "forbid", name
        assert set(model.model_fields) == fields, name


@pytest.mark.parametrize(
    ("tool", "field"),
    [
        ("write_file", "path"),
        ("create_folder", "path"),
        ("move_path", "from_path"),
        ("move_path", "to_path"),
        ("delete_path", "path"),
    ],
)
def test_write_tool_paths_are_capped_at_1024_characters(tool: str, field: str) -> None:
    """A relative path longer than the contract allows never reaches a precheck."""
    registry = write_registry()
    raw_input = dict(WRITE_CALLS[tool])
    raw_input[field] = "x" * 1025
    model = registry.get(tool).input_model

    with pytest.raises(ValidationError):
        model.model_validate(raw_input)


@pytest.mark.parametrize("tool", sorted(WRITE_CALLS))
async def test_write_tool_prechecks_refuse_an_offline_workspace(
    seeded_users: dict[str, CurrentUser], tool: str
) -> None:
    """Offline is a precheck refusal for every write, before any connector call."""
    user = seeded_users["viewer@acme.test"]
    spec = write_registry().get(tool)
    requester = FakeRequester(online=False)
    payload = spec.input_model.model_validate(WRITE_CALLS[tool])

    with pytest.raises(ToolPrecheckError) as exc:
        await spec.precheck(context(requester, user), payload)

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
async def test_write_tools_reject_paths_outside_the_workspace(
    seeded_users: dict[str, CurrentUser], path: str
) -> None:
    """Every path field of every write applies the same jail rules."""
    user = seeded_users["viewer@acme.test"]
    registry = write_registry()
    cases = (
        ("write_file", {"path": path, "content": "hola"}),
        ("create_folder", {"path": path}),
        ("move_path", {"from_path": path, "to_path": "target.txt"}),
        ("move_path", {"from_path": "source.txt", "to_path": path}),
        ("delete_path", {"path": path}),
    )

    for tool, raw_input in cases:
        spec = registry.get(tool)
        payload = spec.input_model.model_validate(raw_input)
        with pytest.raises(ToolPrecheckError) as exc:
            await spec.precheck(context(FakeRequester(), user), payload)
        assert exc.value.reason == INVALID_PATH


async def test_write_file_accepts_fifty_thousand_characters_and_rejects_one_more(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """The content limit is exactly 50 000 characters, refused with the contract text."""
    user = seeded_users["viewer@acme.test"]
    spec = write_registry().get("write_file")
    requester = FakeRequester(result={"path": "long.txt", "bytes": 0, "created": True})

    await spec.precheck(
        context(requester, user),
        spec.input_model.model_validate({"path": "long.txt", "content": "a" * MAX_TEXT_CHARS}),
    )
    assert requester.calls == []

    with pytest.raises(ToolPrecheckError) as exc:
        await spec.precheck(
            context(requester, user),
            spec.input_model.model_validate(
                {"path": "long.txt", "content": "a" * (MAX_TEXT_CHARS + 1)}
            ),
        )
    assert exc.value.reason == CONTENT_TOO_LARGE
    assert requester.calls == []


@pytest.mark.parametrize(
    ("online", "raw_input", "reason"),
    [
        (False, {"path": "notes.txt", "content": "hola"}, OFFLINE),
        (True, {"path": "../secret.txt", "content": "hola"}, INVALID_PATH),
        (True, {"path": "big.txt", "content": "a" * (MAX_TEXT_CHARS + 1)}, CONTENT_TOO_LARGE),
    ],
)
async def test_a_refused_write_precheck_never_reaches_the_approval_gate(
    seeded_db: Database,
    seeded_users: dict[str, CurrentUser],
    online: bool,
    raw_input: dict,
    reason: str,
) -> None:
    """No approval request exists for an offline device, a bad path, or too much text."""
    registry = write_registry()
    requester = FakeRequester(online=online)
    user = seeded_users["viewer@acme.test"]

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            registry, session, user, "write_file", raw_input, connectors=requester
        )

    assert result.status == "error"
    assert result.error_kind == "precheck"
    assert result.error == f"precheck_failed: {reason}"
    assert result.approval_request is None
    assert requester.calls == []


async def test_write_file_creates_or_overwrites_through_the_connector(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """The server forwards path and content verbatim and echoes the byte count."""
    user = seeded_users["viewer@acme.test"]
    requester = FakeRequester(result={"path": "docs/notes.txt", "bytes": 4, "created": True})

    output = await connector_tools.write_file(
        context(requester, user),
        connector_tools.WriteFileInput(path="docs/notes.txt", content="hola"),
    )

    assert (output.path, output.bytes, output.created) == ("docs/notes.txt", 4, True)
    assert requester.calls == [
        (user.id, "write_file", {"path": "docs/notes.txt", "content": "hola"}, 30.0)
    ]


async def test_create_folder_and_move_path_forward_the_relative_paths(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """Folder creation and moves send exactly their contract arguments."""
    user = seeded_users["viewer@acme.test"]

    folder_requester = FakeRequester(result={"path": "docs"})
    folder = await connector_tools.create_folder(
        context(folder_requester, user), connector_tools.CreateFolderInput(path="docs")
    )
    assert folder.path == "docs"
    assert folder_requester.calls == [(user.id, "create_folder", {"path": "docs"}, 30.0)]

    move_requester = FakeRequester(result={"from_path": "a.txt", "to_path": "b.txt"})
    moved = await connector_tools.move_path(
        context(move_requester, user),
        connector_tools.MovePathInput(from_path="a.txt", to_path="b.txt"),
    )
    assert (moved.from_path, moved.to_path) == ("a.txt", "b.txt")
    assert move_requester.calls == [
        (user.id, "move_path", {"from_path": "a.txt", "to_path": "b.txt"}, 30.0)
    ]


async def test_delete_path_returns_the_number_of_removed_entries(
    seeded_users: dict[str, CurrentUser],
) -> None:
    """A recursive delete reports how many entries the connector removed."""
    user = seeded_users["viewer@acme.test"]
    requester = FakeRequester(result={"path": "docs", "removed": 3})

    output = await connector_tools.delete_path(
        context(requester, user), connector_tools.DeletePathInput(path="docs")
    )

    assert (output.path, output.removed) == ("docs", 3)
    assert requester.calls == [(user.id, "delete_path", {"path": "docs"}, 30.0)]


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        ("create_folder", {"path": "docs"}),
        ("move_path", {"from_path": "a.txt", "to_path": "b.txt"}),
    ],
)
async def test_already_exists_is_a_readable_refusal(
    seeded_users: dict[str, CurrentUser], tool: str, payload: dict
) -> None:
    """The connector decides existence; the server only translates its code."""
    user = seeded_users["viewer@acme.test"]
    spec = write_registry().get(tool)
    requester = FakeRequester(error=ConnectorResponseError("already_exists", "exists"))

    with pytest.raises(ToolPrecheckError) as exc:
        await getattr(connector_tools, tool)(
            context(requester, user), spec.input_model.model_validate(payload)
        )

    assert exc.value.reason == ALREADY_EXISTS


def test_existing_read_tools_keep_their_auto_extract_declaration() -> None:
    """Registering the four writes leaves the L1 reads auto-approved and read-only."""
    registry = write_registry()

    for name in ("list_files", "read_file"):
        spec = registry.get(name)
        assert spec.kind is ToolKind.EXTRACT
        assert spec.approval_policy is ApprovalPolicy.AUTO
        assert spec.requester_only_approval is False
        assert spec.precheck is not None


@pytest.mark.parametrize("tool", sorted(WRITE_CALLS))
async def test_a_write_without_a_grant_asks_for_approval_and_touches_nothing(
    seeded_db: Database, seeded_users: dict[str, CurrentUser], tool: str
) -> None:
    """Every write stops at ``approval_required``; the connector is never called."""
    registry = write_registry()
    requester = FakeRequester(result={})
    user = seeded_users["viewer@acme.test"]

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            registry, session, user, tool, WRITE_CALLS[tool], connectors=requester
        )

    assert result.status == "approval_required"
    assert result.approval_request is not None
    assert result.approval_request.tool_name == tool
    assert result.approval_request.input == WRITE_CALLS[tool]
    assert result.approval_request.required_role == "viewer"
    assert requester.calls == []


async def test_an_approved_write_executes_exactly_the_approved_input(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """The grant the approvals service mints runs the stored payload once."""
    registry = write_registry()
    viewer = seeded_users["viewer@acme.test"]
    approved_input = {"path": "notes.txt", "content": "hola"}
    requester = FakeRequester(result={"path": "notes.txt", "bytes": 4, "created": True})
    request_id = await _request_approval(seeded_db, registry, viewer, "write_file", approved_input)
    await _approve(seeded_db, registry, viewer, request_id)

    outcome = await _execute_approved(seeded_db, registry, viewer, request_id, requester)

    assert outcome.request.status is ApprovalStatus.EXECUTED
    assert outcome.request.execution_result == {
        "path": "notes.txt",
        "bytes": 4,
        "created": True,
    }
    assert outcome.result.status == "ok"
    assert outcome.result.output.model_dump() == {
        "path": "notes.txt",
        "bytes": 4,
        "created": True,
    }
    assert requester.calls == [(viewer.id, "write_file", approved_input, 30.0)]


async def test_only_the_requester_may_approve_a_write(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """The granted folder belongs to the requester: another user cannot approve."""
    registry = write_registry()
    viewer = seeded_users["viewer@acme.test"]
    admin = seeded_users["admin@acme.test"]
    requester = FakeRequester(result={})
    request_id = await _request_approval(
        seeded_db, registry, viewer, "write_file", {"path": "notes.txt", "content": "hola"}
    )

    with pytest.raises(ApprovalForbiddenError):
        await _approve(seeded_db, registry, admin, request_id)

    assert requester.calls == []
    async with seeded_db.sessionmaker() as session:
        stored = await get_approval_request(session, viewer, request_id)
    assert stored.status is ApprovalStatus.PENDING


async def test_a_rejected_write_executes_nothing(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """Rejection ends the request; the connector never sees the call."""
    registry = write_registry()
    viewer = seeded_users["viewer@acme.test"]
    requester = FakeRequester(result={})
    request_id = await _request_approval(
        seeded_db, registry, viewer, "write_file", {"path": "notes.txt", "content": "hola"}
    )
    async with seeded_db.sessionmaker() as session:
        await reject_request(session, viewer, request_id, reason="no", now=T0, registry=registry)

    with pytest.raises(ApprovalStateError):
        await _execute_approved(seeded_db, registry, viewer, request_id, requester)

    assert requester.calls == []
    async with seeded_db.sessionmaker() as session:
        stored = await get_approval_request(session, viewer, request_id)
    assert stored.status is ApprovalStatus.REJECTED


async def test_an_expired_write_executes_nothing(
    seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """Expiry is detected at execution time; the connector never sees the call."""
    registry = write_registry()
    viewer = seeded_users["viewer@acme.test"]
    requester = FakeRequester(result={})
    request_id = await _request_approval(
        seeded_db, registry, viewer, "write_file", {"path": "notes.txt", "content": "hola"}
    )
    await _approve(seeded_db, registry, viewer, request_id)

    with pytest.raises(ApprovalExpiredError):
        await _execute_approved(
            seeded_db,
            registry,
            viewer,
            request_id,
            requester,
            now=T0 + timedelta(seconds=TTL_SECONDS + 1),
        )

    assert requester.calls == []
    async with seeded_db.sessionmaker() as session:
        stored = await get_approval_request(session, viewer, request_id)
    assert stored.status is ApprovalStatus.EXPIRED
