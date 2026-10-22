"""Independent V2 verification probes for agent-workspace-local L2 (S6/S7).

Derived from the contract in odd/tasks/agent-workspace-local.md, not from the
writer's tests, and driven end to end: POST /api/v1/chat with a scripted model,
then /api/v1/approvals/{id} approve/reject/execute, with a fake connector hub
on ``app.state.connectors``. They pin that a write never runs without the
requester's approval, runs exactly what was approved once, never creates an
approval for an offline workspace or an invalid path, and that the
connector's refusals reach the human and the model as readable messages.
"""

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.agent.llm import AssistantMessage, FakeLLMProvider, ToolCall
from app.modules.approvals.models import ApprovalRequest
from app.modules.connectors.hub import ConnectorResponseError
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.retrieval.embeddings import FakeEmbeddingProvider

DIM = 64
SETTINGS = Settings(
    jwt_secret="test-secret-0123456789-abcdefghijklmnop",
    access_token_expire_minutes=30,
    embedding_provider="fake",
    embedding_dim=DIM,
    llm_provider="fake",
    agent_max_steps=4,
)
OFFLINE = "El espacio de trabajo no está conectado. Abre el widget y conecta tu carpeta."
INVALID_PATH = "La ruta no es válida. Usa una ruta relativa dentro de la carpeta concedida."
CONTENT_TOO_LARGE = "El contenido supera el límite de 50 000 caracteres."
ALREADY_EXISTS = "Ya existe un elemento con ese nombre en la carpeta concedida."
BASE = "/api/v1/approvals"
#: tool -> (approved args, fake connector result, expected execution_result)
WRITES = {
    "write_file": (
        {"path": "notas/plan.md", "content": "línea 1\nlínea 2 ✓"},
        {"path": "notas/plan.md", "bytes": 22, "created": True},
        {"path": "notas/plan.md", "bytes": 22, "created": True},
    ),
    "create_folder": ({"path": "nueva"}, {"path": "nueva"}, {"path": "nueva"}),
    "move_path": ({"from_path": "a.md", "to_path": "sub/b.md"},) * 3,
    "delete_path": (
        {"path": "vieja"},
        {"path": "vieja", "removed": 3},
        {"path": "vieja", "removed": 3},
    ),
}


class FakeHub:
    """Connector hub double: online switch, preset result or error, recorded calls."""

    def __init__(self, online=True, result=None, error=None):
        self.online = online
        self.result = {} if result is None else result
        self.error = error
        self.calls = []

    def is_online(self, user_id):
        return self.online

    async def request(self, user_id, op, args, timeout=30.0):
        self.calls.append((user_id, op, args))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def v2_app(seeded_db: Database, tmp_path: Path) -> FastAPI:
    application = create_app()
    application.state.db = seeded_db
    application.state.settings = SETTINGS
    application.state.embeddings = FakeEmbeddingProvider(dimension=DIM)
    application.state.storage = LocalDocumentStorage(tmp_path)
    return application


@pytest.fixture
async def v2_client(v2_app: FastAPI) -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=v2_app), base_url="http://test") as c:
        yield c


async def _headers(client, email="viewer@acme.test", password="viewer-pass"):
    slug = email.split("@")[1].split(".")[0]
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password, "tenant_slug": slug}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _chat(app, client, tool, args, hub, headers):
    app.state.connectors = hub
    llm = FakeLLMProvider(
        [
            AssistantMessage(content="", tool_calls=(ToolCall(tool, args),)),
            AssistantMessage(content="Listo."),
        ]
    )
    app.state.llm = llm
    response = await client.post("/api/v1/chat", json={"message": "hazlo"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json(), llm


async def _approval_rows(db):
    async with db.sessionmaker() as session:
        count = select(func.count()).select_from(ApprovalRequest)
        return (await session.execute(count)).scalar_one()


def _tool_message_text(llm):
    return "\n".join(str(message) for message in llm.calls[-1].messages)


async def _approve_and_execute(client, approval_id, headers):
    approved = await client.post(f"{BASE}/{approval_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return await client.post(f"{BASE}/{approval_id}/execute", headers=headers)


@pytest.mark.parametrize("tool", sorted(WRITES))
async def test_v2_write_needs_requester_approval_and_runs_exactly_once(
    v2_app, v2_client, seeded_db, seeded_users, tool
):
    args, connector_result, expected = WRITES[tool]
    hub = FakeHub(result=connector_result)
    viewer = await _headers(v2_client)
    body, _ = await _chat(v2_app, v2_client, tool, args, hub, viewer)
    [pending] = body["pending_approvals"]
    assert pending["tool_name"] == tool and hub.calls == []
    approval_id = pending["approval_id"]
    got = await v2_client.get(f"{BASE}/{approval_id}", headers=viewer)
    assert got.json()["status"] == "pending" and got.json()["input"] == args
    admin = await _headers(v2_client, "admin@acme.test", "admin-pass")
    owner = await _headers(v2_client, "owner@acme.test", "owner-pass")
    for other in (admin, owner):
        refused = await v2_client.post(f"{BASE}/{approval_id}/approve", headers=other)
        assert refused.status_code == 403
    approved = await v2_client.post(f"{BASE}/{approval_id}/approve", headers=viewer)
    assert approved.status_code == 200
    by_admin = await v2_client.post(f"{BASE}/{approval_id}/execute", headers=admin)
    assert by_admin.status_code == 403
    assert hub.calls == []

    executed = await v2_client.post(f"{BASE}/{approval_id}/execute", headers=viewer)

    assert executed.status_code == 200 and executed.json()["status"] == "executed"
    assert executed.json()["execution_result"] == expected
    assert hub.calls == [(seeded_users["viewer@acme.test"].id, tool, args)]
    replay = await v2_client.post(f"{BASE}/{approval_id}/execute", headers=viewer)
    assert replay.status_code == 409
    assert len(hub.calls) == 1


async def test_v2_rejected_write_executes_nothing(v2_app, v2_client, seeded_db):
    hub = FakeHub(result=WRITES["delete_path"][1])
    viewer = await _headers(v2_client)
    body, _ = await _chat(v2_app, v2_client, "delete_path", {"path": "vieja"}, hub, viewer)
    approval_id = body["pending_approvals"][0]["approval_id"]

    rejected = await v2_client.post(f"{BASE}/{approval_id}/reject", headers=viewer)

    assert rejected.status_code == 200
    for verb in ("approve", "execute"):
        after = await v2_client.post(f"{BASE}/{approval_id}/{verb}", headers=viewer)
        assert after.status_code == 409
    assert hub.calls == []


@pytest.mark.parametrize("approve_first", [False, True], ids=["pending", "approved"])
async def test_v2_expired_write_executes_nothing(v2_app, v2_client, seeded_db, approve_first):
    hub = FakeHub(result=WRITES["write_file"][1])
    viewer = await _headers(v2_client)
    body, _ = await _chat(v2_app, v2_client, "write_file", WRITES["write_file"][0], hub, viewer)
    approval_id = body["pending_approvals"][0]["approval_id"]
    if approve_first:
        approved = await v2_client.post(f"{BASE}/{approval_id}/approve", headers=viewer)
        assert approved.status_code == 200
    async with seeded_db.sessionmaker() as session:
        row = await session.get(ApprovalRequest, UUID(approval_id))
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == row.id)
            .values(expires_at=row.created_at - timedelta(days=1))
        )
        await session.commit()

    for verb in ("approve", "execute"):
        after = await v2_client.post(f"{BASE}/{approval_id}/{verb}", headers=viewer)
        assert after.status_code in (409, 410)
    assert hub.calls == []


BAD_PATHS = ["../fuera.txt", "/etc/passwd", "a\x00b.txt", "sub/../../x", "C:\\x.txt", "a" * 1025]
#: The granted root itself is never a write target: the connector always refuses it.
ROOT_PATHS = ["", ".", "./", ".//", "./."]


def _calls_on(paths):
    for bad in paths:
        yield "write_file", {"path": bad, "content": "x"}
        yield "create_folder", {"path": bad}
        yield "delete_path", {"path": bad}
        yield "move_path", {"from_path": bad, "to_path": "ok.txt"}
        yield "move_path", {"from_path": "ok.txt", "to_path": bad}


@pytest.mark.parametrize(("tool", "args"), [*_calls_on(BAD_PATHS), *_calls_on(ROOT_PATHS)])
async def test_v2_invalid_paths_never_create_an_approval(v2_app, v2_client, seeded_db, tool, args):
    hub = FakeHub()
    body, llm = await _chat(v2_app, v2_client, tool, args, hub, await _headers(v2_client))

    assert body["pending_approvals"] == []
    assert await _approval_rows(seeded_db) == 0 and hub.calls == []
    assert body["tool_calls"][0]["status"] == "error"
    # An empty or 1025-character path fails input validation before the precheck.
    if all(0 < len(value) <= 1024 for value in args.values()):
        assert INVALID_PATH in _tool_message_text(llm)


@pytest.mark.parametrize("tool", sorted(WRITES))
async def test_v2_offline_workspace_never_creates_an_approval(v2_app, v2_client, seeded_db, tool):
    hub = FakeHub(online=False)
    body, llm = await _chat(
        v2_app, v2_client, tool, WRITES[tool][0], hub, await _headers(v2_client)
    )

    assert body["pending_approvals"] == []
    assert await _approval_rows(seeded_db) == 0 and hub.calls == []
    assert OFFLINE in _tool_message_text(llm)


async def test_v2_content_cap_is_checked_before_the_approval(v2_app, v2_client, seeded_db):
    viewer, hub = await _headers(v2_client), FakeHub()
    over = {"path": "x.txt", "content": "é" * 50_001}
    body, llm = await _chat(v2_app, v2_client, "write_file", over, hub, viewer)
    assert body["pending_approvals"] == [] and await _approval_rows(seeded_db) == 0
    assert CONTENT_TOO_LARGE in _tool_message_text(llm)

    at_cap = {"path": "x.txt", "content": "é" * 50_000}
    body, _ = await _chat(v2_app, v2_client, "write_file", at_cap, hub, viewer)

    assert len(body["pending_approvals"]) == 1 and hub.calls == []


WRITE_ERRORS = {
    "already_exists": ALREADY_EXISTS,
    "not_found": "No existe ningún elemento con esa ruta en la carpeta concedida.",
    "permission_denied": "No hay permiso para modificar ese elemento de la carpeta concedida.",
    "too_large": "El archivo supera el límite de 5 MB.",
    "invalid_path": INVALID_PATH,
    "io_error": "No se pudo completar la operación en la carpeta concedida.",
}


@pytest.mark.parametrize("tool", sorted(WRITES))
@pytest.mark.parametrize("code", sorted(WRITE_ERRORS))
async def test_v2_connector_errors_are_readable_after_approval(v2_app, v2_client, tool, code):
    viewer = await _headers(v2_client)
    hub = FakeHub(error=ConnectorResponseError(code, "raw connector text"))
    body, _ = await _chat(v2_app, v2_client, tool, WRITES[tool][0], hub, viewer)
    approval_id = body["pending_approvals"][0]["approval_id"]

    executed = await _approve_and_execute(v2_client, approval_id, viewer)

    assert executed.status_code == 200 and executed.json()["status"] == "failed"
    assert executed.json()["execution_error"] == f"precheck_failed: {WRITE_ERRORS[code]}"
    assert "raw connector text" not in executed.text
    assert len(hub.calls) == 1


READ_ERRORS = {
    "not_found": "El archivo no existe.",
    "permission_denied": "No hay permiso para acceder al archivo.",
    "too_large": "El archivo supera el límite de 5 MB.",
    "io_error": "No se pudo leer el archivo.",
}


@pytest.mark.parametrize("code", sorted(READ_ERRORS))
async def test_v2_connector_read_errors_reach_the_model(v2_app, v2_client, code):
    hub = FakeHub(error=ConnectorResponseError(code, "raw connector text"))
    args = {"path": "notas/plan.md"}
    body, llm = await _chat(v2_app, v2_client, "read_file", args, hub, await _headers(v2_client))

    assert body["tool_calls"][0]["status"] == "error"
    assert body["tool_calls"][0]["error"] == "precheck_failed"
    assert READ_ERRORS[code] in _tool_message_text(llm)
    assert "raw connector text" not in _tool_message_text(llm)


async def test_v2_workspace_offline_at_execute_is_readable_and_dispatches_nothing(
    v2_app, v2_client
):
    viewer = await _headers(v2_client)
    hub = FakeHub(result={"path": "n"})
    body, _ = await _chat(v2_app, v2_client, "create_folder", {"path": "n"}, hub, viewer)
    approval_id = body["pending_approvals"][0]["approval_id"]
    approved = await v2_client.post(f"{BASE}/{approval_id}/approve", headers=viewer)
    assert approved.status_code == 200
    hub.online = False

    executed = await v2_client.post(f"{BASE}/{approval_id}/execute", headers=viewer)

    assert executed.json()["status"] == "failed"
    assert executed.json()["execution_error"] == f"precheck_failed: {OFFLINE}"
    assert hub.calls == []
