"""Independent V1 verification probes for agent-workspace-local L1.

These probes are derived from the verbatim contract in
odd/tasks/agent-workspace-local.md, not from the writer's tests: path-jail
rejections, the exact offline message, WS 4401, pairing-code contract,
size caps, and the "server never sees a path" invariant (S1/S2/S5).
"""

import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

from app.core.db import Database
from app.modules.connectors.models import Connector, ConnectorPairingCode
from app.modules.connectors.router import connector_ws
from app.modules.connectors.tools import (
    ListFilesInput,
    ReadFileInput,
    list_files,
    read_file,
    register_connector_tools,
)
from app.modules.identity.models import UserRole
from app.modules.tools.exceptions import ToolPrecheckError
from app.modules.tools.executor import execute_tool
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolSpec,
)

PAIRING_CODES = "/api/v1/connectors/pairing-codes"
PAIR = "/api/v1/connectors/pair"
CONNECTORS = "/api/v1/connectors"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PATTERN = re.compile(rf"^[{ALPHABET}]{{4}}-[{ALPHABET}]{{4}}$")
OFFLINE = "El espacio de trabajo no está conectado. Abre el widget y conecta tu carpeta."
INVALID_PATH = "La ruta no es válida. Usa una ruta relativa dentro de la carpeta concedida."
TOO_LARGE = "El archivo supera el límite de 5 MB."


class Recorder:
    """Fake ConnectorRequester that records every request it receives."""

    def __init__(self, online=True, result=None):
        self.online = online
        self.result = {} if result is None else result
        self.calls = []

    def is_online(self, user_id):
        return self.online

    async def request(self, user_id, op, args, timeout=30.0):
        self.calls.append((user_id, op, args))
        return self.result


def ctx(connectors, user):
    return ToolContext(session=None, tenant_id=user.tenant_id, user=user, connectors=connectors)


async def headers(client, email="viewer@acme.test", password="viewer-pass"):
    slug = email.split("@")[1].split(".")[0]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": slug},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- S5 / Contrato: path jail at the server edge -----------------------------

JAIL_CASES = [
    "../etc/passwd",
    "/etc/passwd",
    "sub/../../secret.md",
    "C:\\Windows\\win.ini",
    "a\x00b.md",
]


async def test_probe_path_jail_rejects_and_never_reaches_the_connector(seeded_db, seeded_users):
    """Every escape is refused through the real executor path with the exact message."""
    user = seeded_users["viewer@acme.test"]
    registry = ToolRegistry()
    register_connector_tools(registry)
    async with seeded_db.sessionmaker() as session:
        for bad in JAIL_CASES:
            recorder = Recorder(result={"entries": []})
            result = await execute_tool(
                registry, session, user, "list_files", {"path": bad}, connectors=recorder
            )
            assert result.status == "error", (bad, result.status)
            assert result.error == f"precheck_failed: {INVALID_PATH}", (bad, result.error)
            assert recorder.calls == [], f"rejected path reached the connector: {bad!r}"


async def test_probe_read_file_rejects_jail_escapes_before_request(seeded_db, seeded_users):
    user = seeded_users["viewer@acme.test"]
    registry = ToolRegistry()
    register_connector_tools(registry)
    async with seeded_db.sessionmaker() as session:
        for bad in ["../outside.md", "/etc/passwd", "a\x00b.txt"]:
            recorder = Recorder(result={"data_b64": "", "size": 0})
            result = await execute_tool(
                registry, session, user, "read_file", {"path": bad}, connectors=recorder
            )
            assert result.status == "error", (bad, result.status)
            assert result.error == f"precheck_failed: {INVALID_PATH}", (bad, result.error)
            assert recorder.calls == []


# --- S2: offline message verbatim + rest of the agent keeps working ----------


async def test_probe_offline_message_is_verbatim(seeded_db, seeded_users):
    user = seeded_users["viewer@acme.test"]
    offline = Recorder(online=False)
    for tool, input_model in (
        (list_files, ListFilesInput(path="")),
        (read_file, ReadFileInput(path="a.txt")),
    ):
        try:
            await tool(ctx(offline, user), input_model)
        except ToolPrecheckError as exc:
            assert str(exc) == OFFLINE, repr(str(exc))
        else:
            raise AssertionError("offline workspace was accepted")


async def test_probe_other_tools_keep_working_while_workspace_is_offline(seeded_db, seeded_users):
    user = seeded_users["viewer@acme.test"]
    registry = ToolRegistry()
    register_connector_tools(registry)

    async def echo(context, validated_input):
        return {"pong": True}

    class NoInput(BaseModel):
        pass

    registry.register(
        ToolSpec(
            name="v1_probe_echo",
            description="probe-only tool proving the agent keeps working offline",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.VIEWER,
            handler=echo,
            input_model=NoInput,
        )
    )
    async with seeded_db.sessionmaker() as session:
        offline = await execute_tool(
            registry, session, user, "list_files", {}, connectors=Recorder(online=False)
        )
        other = await execute_tool(
            registry, session, user, "v1_probe_echo", {}, connectors=Recorder(online=False)
        )
    assert offline.status == "error"
    assert offline.error == f"precheck_failed: {OFFLINE}"
    assert other.status == "ok", other.error


# --- S1 / Contrato: WebSocket 4401 -------------------------------------------


class FakeWS:
    def __init__(self, app, token=None):
        self.app = app
        self.headers = Headers({} if token is None else {"authorization": f"Bearer {token}"})
        self.accepted = False
        self.closed = []
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        self.closed.append(code)

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_json(self):
        raise WebSocketDisconnect(code=1000)


async def test_probe_ws_closes_4401_without_token(identity_app, seeded_db, seeded_users):
    ws = FakeWS(identity_app)
    await connector_ws(ws)
    assert ws.closed == [4401]
    assert not ws.accepted


async def test_probe_ws_closes_4401_with_wrong_token(identity_app, seeded_db, seeded_users):
    ws = FakeWS(identity_app, token="not-a-real-token")
    await connector_ws(ws)
    assert ws.closed == [4401]


async def test_probe_ws_closes_4401_with_revoked_token(identity_app, seeded_db, seeded_users):
    user = seeded_users["viewer@acme.test"]
    async with seeded_db.sessionmaker() as session:
        connector = Connector(
            tenant_id=user.tenant_id,
            user_id=user.id,
            name="revoked",
            token_hash=hashlib.sha256(b"revoked-token").hexdigest(),
            revoked_at=datetime.now(tz=UTC),
        )
        session.add(connector)
        await session.commit()
    ws = FakeWS(identity_app, token="revoked-token")
    await connector_ws(ws)
    assert ws.closed == [4401]


# --- S1 / Contrato: pairing code contract ------------------------------------


async def test_probe_pairing_code_shape_ttl_and_hash_only_storage(
    identity_client, seeded_db, seeded_users
):
    response = await identity_client.post(PAIRING_CODES, headers=await headers(identity_client))
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"code", "expires_at"}, body
    assert PATTERN.fullmatch(body["code"]), body["code"]
    remaining = datetime.fromisoformat(body["expires_at"]) - datetime.now(tz=UTC)
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10, seconds=5)
    normalized = body["code"].replace("-", "")
    async with seeded_db.sessionmaker() as session:
        rows = list((await session.execute(select(ConnectorPairingCode))).scalars())
    assert len(rows) == 1
    assert rows[0].code_hash == hashlib.sha256(normalized.encode()).hexdigest()
    assert normalized not in rows[0].code_hash


async def test_probe_second_code_invalidates_the_first(identity_client, seeded_db):
    h = await headers(identity_client)
    first = (await identity_client.post(PAIRING_CODES, headers=h)).json()["code"]
    second = (await identity_client.post(PAIRING_CODES, headers=h)).json()["code"]
    rejected = await identity_client.post(PAIR, json={"code": first, "name": "Old"})
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "Código inválido o vencido"}
    accepted = await identity_client.post(PAIR, json={"code": second, "name": "New"})
    assert accepted.status_code == 201, accepted.text


async def test_probe_failed_pairing_leaves_state_byte_identical(identity_client, seeded_db):
    before = await _digest(seeded_db)
    for payload in (
        {"code": "ZZZZ-ZZZZ", "name": "X"},
        {"code": "", "name": "X"},
        {"code": "ABCDEFGH", "name": ""},
        {"code": "ABCDEFGH", "name": "x" * 81},
        {"code": "ABCDEFGH", "name": "X", "extra": "forbidden"},
    ):
        response = await identity_client.post(PAIR, json=payload)
        assert response.status_code in (400, 422), (payload, response.status_code)
        if response.status_code == 400:
            assert response.json() == {"detail": "Código inválido o vencido"}
    assert await _digest(seeded_db) == before


async def _digest(db: Database) -> str:
    """Deterministic dump of every connector-related row."""
    async with db.sessionmaker() as session:
        codes = [
            (r.id, r.code_hash, r.used_at, r.revoked_at, r.expires_at)
            for r in (await session.execute(select(ConnectorPairingCode))).scalars()
        ]
        connectors = [
            (r.id, r.name, r.token_hash, r.revoked_at)
            for r in (await session.execute(select(Connector))).scalars()
        ]
    return repr((codes, connectors))


async def test_probe_list_and_reject_views_never_expose_a_path(
    identity_client, seeded_db, seeded_users
):
    """The server has no path to leak: API views and DB columns carry none (S1)."""
    h = await headers(identity_client)
    code = (await identity_client.post(PAIRING_CODES, headers=h)).json()["code"]
    paired = await identity_client.post(PAIR, json={"code": code, "name": "Equipo"})
    assert paired.status_code == 201
    listing = await identity_client.get(CONNECTORS, headers=h)
    assert listing.status_code == 200
    text = listing.text
    for marker in ("folder", "path", "ruta", "carpeta", "/home", "/etc"):
        assert marker not in text.lower(), marker
    columns = {c.name for c in Connector.__table__.columns}
    required = {
        "id",
        "tenant_id",
        "user_id",
        "name",
        "token_hash",
        "created_at",
        "last_seen_at",
        "revoked_at",
    }
    assert required <= columns, columns
    assert not any("path" in c or "folder" in c or "dir" in c for c in columns), columns


# --- S5 caps -----------------------------------------------------------------


def _b64(data: bytes) -> dict:
    return {"data_b64": base64.b64encode(data).decode(), "size": len(data)}


async def test_probe_read_file_caps_size_and_text(seeded_db, seeded_users):
    user = seeded_users["viewer@acme.test"]

    big = Recorder(result={"data_b64": "A" * (5 * 1024 * 1024 + 100), "size": 6_000_000})
    try:
        await read_file(ctx(big, user), ReadFileInput(path="big.txt"))
    except ToolPrecheckError as exc:
        assert str(exc) == TOO_LARGE, str(exc)
    else:
        raise AssertionError("oversized read accepted")

    clip = Recorder(result=_b64(b"x" * 60_000))
    out = await read_file(ctx(clip, user), ReadFileInput(path="long.txt"))
    assert out.truncated is True
    assert len(out.text) == 50_000
    assert out.size == 60_000


async def test_probe_list_files_caps_at_1000(seeded_db, seeded_users):
    user = seeded_users["viewer@acme.test"]
    entries = [{"name": f"f{i}", "path": f"f{i}", "kind": "file", "size": 1} for i in range(1500)]
    recorder = Recorder(result={"entries": entries})
    out = await list_files(ctx(recorder, user), ListFilesInput(path=""))
    assert len(out.entries) == 1000
