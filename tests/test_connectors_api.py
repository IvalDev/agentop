"""Connector pairing and management API tests.

Contract L1 (agent-workspace-local S1/S2): a user mints a short-lived pairing
code, the local connector redeems it for a long-lived token, and the user can
list and revoke their own connectors. Only the owner sees a connector; the
WebSocket connection state comes from the in-process hub.

These tests use the shared in-memory SQLite fixtures: the whole ORM schema is
created with ``Base.metadata.create_all`` (the repo has no Alembic revisions).
"""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.core.db import Database
from app.modules.agent.router import get_agent_deps
from app.modules.connectors.models import Connector, ConnectorPairingCode
from app.modules.identity.deps import CurrentUser
from app.modules.identity.security import hash_refresh_token

PAIRING_CODES = "/api/v1/connectors/pairing-codes"
PAIR = "/api/v1/connectors/pair"
CONNECTORS = "/api/v1/connectors"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_PATTERN = re.compile(rf"^[{CODE_ALPHABET}]{{4}}-[{CODE_ALPHABET}]{{4}}$")
INVALID_CODE_DETAIL = "Código inválido o vencido"


async def _headers(
    client: AsyncClient, email: str = "viewer@acme.test", password: str = "viewer-pass"
) -> dict[str, str]:
    tenant_slug = email.split("@")[1].split(".")[0]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _normalize(code: str) -> str:
    return code.replace("-", "")


async def _mint_code(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(PAIRING_CODES, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["code"]


async def _code_rows(db: Database) -> list[ConnectorPairingCode]:
    async with db.sessionmaker() as session:
        result = await session.execute(
            select(ConnectorPairingCode).order_by(ConnectorPairingCode.id)
        )
        return list(result.scalars())


async def _connector_rows(db: Database) -> list[Connector]:
    async with db.sessionmaker() as session:
        result = await session.execute(select(Connector).order_by(Connector.created_at))
        return list(result.scalars())


async def _insert_connector(
    db: Database, user: CurrentUser, name: str, raw_token: str | None = None
) -> Connector:
    async with db.sessionmaker() as session:
        connector = Connector(
            id=uuid4(),
            tenant_id=user.tenant_id,
            user_id=user.id,
            name=name,
            token_hash=hash_refresh_token(raw_token or f"token-{name}"),
        )
        session.add(connector)
        await session.commit()
        return connector


class _Socket:
    """Minimal socket the hub can register and close."""

    def __init__(self) -> None:
        self.closed: list[int] = []

    async def send_json(self, data: dict) -> None:  # pragma: no cover - never pinged here
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append(code)


async def test_creating_a_pairing_code_returns_a_formatted_code_stored_only_as_a_hash(
    identity_client: AsyncClient, seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """A viewer may mint a code; the database keeps its digest, never the code."""
    user = seeded_users["viewer@acme.test"]
    response = await identity_client.post(PAIRING_CODES, headers=await _headers(identity_client))

    assert response.status_code == 201, response.text
    body = response.json()
    code = body["code"]
    assert CODE_PATTERN.fullmatch(code), code
    expires_at = datetime.fromisoformat(body["expires_at"])
    remaining = expires_at - datetime.now(tz=UTC)
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10, seconds=5)

    [row] = await _code_rows(seeded_db)
    assert row.user_id == user.id
    assert row.tenant_id == user.tenant_id
    assert row.code_hash == hashlib.sha256(_normalize(code).encode()).hexdigest()
    assert row.code_hash != _normalize(code)
    assert row.used_at is None
    assert row.revoked_at is None


async def test_creating_a_new_code_invalidates_the_previous_unused_code(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """One active code per user: the previous unused code stops working."""
    headers = await _headers(identity_client)
    first = (await identity_client.post(PAIRING_CODES, headers=headers)).json()["code"]
    second = (await identity_client.post(PAIRING_CODES, headers=headers)).json()["code"]

    first_row, second_row = await _code_rows(seeded_db)
    assert first_row.revoked_at is not None
    assert second_row.revoked_at is None

    rejected = await identity_client.post(PAIR, json={"code": first, "name": "Old"})
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": INVALID_CODE_DETAIL}

    accepted = await identity_client.post(PAIR, json={"code": second, "name": "Current"})
    assert accepted.status_code == 201, accepted.text


async def test_an_expired_pairing_code_is_rejected(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """A code past its ten-minute TTL cannot be redeemed."""
    code = await _mint_code(identity_client, await _headers(identity_client))
    async with seeded_db.sessionmaker() as session:
        row = (
            await session.execute(
                select(ConnectorPairingCode).where(
                    ConnectorPairingCode.code_hash
                    == hashlib.sha256(_normalize(code).encode()).hexdigest()
                )
            )
        ).scalar_one()
        row.expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        await session.commit()

    response = await identity_client.post(PAIR, json={"code": code, "name": "Expired"})

    assert response.status_code == 400
    assert response.json() == {"detail": INVALID_CODE_DETAIL}
    assert await _connector_rows(seeded_db) == []


async def test_pairing_is_single_use_and_returns_the_raw_token_once(
    identity_client: AsyncClient, seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """Redeeming a code creates the owner's connector and burns the code."""
    user = seeded_users["viewer@acme.test"]
    code = await _mint_code(identity_client, await _headers(identity_client))

    response = await identity_client.post(PAIR, json={"code": code, "name": "Mi equipo"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Mi equipo"
    assert body["token"]
    assert body["connector_id"]

    [connector] = await _connector_rows(seeded_db)
    assert str(connector.id) == body["connector_id"]
    assert connector.user_id == user.id
    assert connector.tenant_id == user.tenant_id
    assert connector.name == "Mi equipo"
    assert connector.token_hash == hashlib.sha256(body["token"].encode()).hexdigest()
    assert connector.token_hash != body["token"]
    assert connector.last_seen_at is None
    assert connector.revoked_at is None

    reused = await identity_client.post(PAIR, json={"code": code, "name": "Again"})
    assert reused.status_code == 400
    assert reused.json() == {"detail": INVALID_CODE_DETAIL}
    assert len(await _connector_rows(seeded_db)) == 1


@pytest.mark.parametrize("code", ["ZZZZ-ZZZZ", "", "not-a-code", "ABCDEFGH"])
async def test_pair_rejects_unknown_or_malformed_codes(
    identity_client: AsyncClient, seeded_db: Database, code: str
) -> None:
    """Every unknown or malformed code shares the same 400 and creates nothing."""
    response = await identity_client.post(PAIR, json={"code": code, "name": "Nope"})

    assert response.status_code == 400
    assert response.json() == {"detail": INVALID_CODE_DETAIL}
    assert await _connector_rows(seeded_db) == []


async def test_pairing_requires_no_authentication_and_belongs_to_the_code_owner(
    identity_client: AsyncClient, seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """The code is the secret: no session needed, and the connector is the owner's."""
    viewer = seeded_users["viewer@acme.test"]
    code = await _mint_code(identity_client, await _headers(identity_client))

    response = await identity_client.post(PAIR, json={"code": code, "name": "Viewer laptop"})

    assert response.status_code == 201, response.text
    [connector] = await _connector_rows(seeded_db)
    assert (connector.user_id, connector.tenant_id) == (viewer.id, viewer.tenant_id)

    other = await identity_client.get(
        CONNECTORS,
        headers=await _headers(identity_client, "owner@globex.test", "other-pass"),
    )
    assert other.json() == {"connectors": []}


async def test_connector_endpoints_require_authentication(identity_client: AsyncClient) -> None:
    """Minting, listing, and revoking all need a session; pairing does not."""
    assert (await identity_client.post(PAIRING_CODES)).status_code == 401
    assert (await identity_client.get(CONNECTORS)).status_code == 401
    assert (await identity_client.delete(f"{CONNECTORS}/{uuid4()}")).status_code == 401


async def test_listing_returns_only_the_callers_connectors(
    identity_client: AsyncClient, seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """Each user sees their own devices, newest first, with the public fields only."""
    viewer = seeded_users["viewer@acme.test"]
    other = seeded_users["owner@globex.test"]
    await _insert_connector(seeded_db, viewer, "viewer-a")
    await _insert_connector(seeded_db, viewer, "viewer-b")
    await _insert_connector(seeded_db, other, "globex-only")

    response = await identity_client.get(CONNECTORS, headers=await _headers(identity_client))

    assert response.status_code == 200, response.text
    rows = response.json()["connectors"]
    assert {row["name"] for row in rows} == {"viewer-a", "viewer-b"}
    assert all(row["connected"] is False for row in rows)
    for row in rows:
        assert set(row) == {"id", "name", "connected", "last_seen_at", "created_at"}


async def test_listing_reports_connection_state_from_the_hub(
    identity_client: AsyncClient,
    identity_app: FastAPI,
    seeded_db: Database,
    seeded_users: dict[str, CurrentUser],
) -> None:
    """``connected`` is the hub's live WebSocket state, not a database column."""
    viewer = seeded_users["viewer@acme.test"]
    connector = await _insert_connector(seeded_db, viewer, "online-laptop")
    socket = _Socket()
    await identity_app.state.connectors.register(connector.id, viewer.id, socket)

    headers = await _headers(identity_client)
    online = await identity_client.get(CONNECTORS, headers=headers)
    assert online.json()["connectors"][0]["connected"] is True

    identity_app.state.connectors.unregister(connector.id, socket)
    offline = await identity_client.get(CONNECTORS, headers=headers)
    assert offline.json()["connectors"][0]["connected"] is False


async def test_revoking_a_connector_marks_it_and_disconnects_it(
    identity_client: AsyncClient,
    identity_app: FastAPI,
    seeded_db: Database,
    seeded_users: dict[str, CurrentUser],
) -> None:
    """The owner revokes: 204, ``revoked_at`` set, socket closed, row no longer listed."""
    viewer = seeded_users["viewer@acme.test"]
    connector = await _insert_connector(seeded_db, viewer, "old-laptop")
    socket = _Socket()
    await identity_app.state.connectors.register(connector.id, viewer.id, socket)
    headers = await _headers(identity_client)

    response = await identity_client.delete(f"{CONNECTORS}/{connector.id}", headers=headers)

    assert response.status_code == 204
    assert response.content == b""
    async with seeded_db.sessionmaker() as session:
        stored = await session.get(Connector, connector.id)
        assert stored.revoked_at is not None
    assert identity_app.state.connectors.is_connected(connector.id) is False
    assert socket.closed == [1000]
    listed = await identity_client.get(CONNECTORS, headers=headers)
    assert listed.json() == {"connectors": []}
    again = await identity_client.delete(f"{CONNECTORS}/{connector.id}", headers=headers)
    assert again.status_code == 404


async def test_revoking_someone_elses_connector_is_not_found(
    identity_client: AsyncClient, seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """Foreign and unknown ids share one 404 and leave the row untouched."""
    other = seeded_users["owner@globex.test"]
    connector = await _insert_connector(seeded_db, other, "globex-laptop")
    headers = await _headers(identity_client)

    foreign = await identity_client.delete(f"{CONNECTORS}/{connector.id}", headers=headers)
    unknown = await identity_client.delete(f"{CONNECTORS}/{uuid4()}", headers=headers)

    assert foreign.status_code == unknown.status_code == 404
    async with seeded_db.sessionmaker() as session:
        stored = await session.get(Connector, connector.id)
        assert stored.revoked_at is None


def test_agent_dependencies_carry_the_process_connector_hub(identity_app: FastAPI) -> None:
    """The chat router threads ``app.state.connectors`` into every run's deps."""
    request = SimpleNamespace(app=identity_app)

    deps = get_agent_deps(
        request,
        identity_app.state.settings,
        identity_app.state.embeddings,
        identity_app.state.storage,
    )

    assert deps.connectors is identity_app.state.connectors
