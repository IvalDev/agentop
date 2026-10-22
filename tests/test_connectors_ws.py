"""Connector WebSocket authentication tests.

The endpoint reads ``Authorization: Bearer <connector token>`` from the
WebSocket handshake (never cookies), looks the token hash up in the database,
and closes with 4401 when it is missing, unknown, or revoked. These tests call
the route function with a fake WebSocket, so they exercise the real auth,
database session, hub registration, and ``ready`` frame without a live ASGI
WebSocket client.

Gap: the actual handshake upgrade and the 30-second application ping are not
covered here (``httpx.ASGITransport`` has no WebSocket support and the shared
in-memory SQLite database lives on the test event loop); the hub transport is
covered in ``test_connectors_hub``.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

from app.core.db import Database
from app.modules.connectors.models import Connector
from app.modules.connectors.router import connector_ws
from app.modules.identity.deps import CurrentUser
from app.modules.identity.security import hash_refresh_token

WS_URL = "/api/v1/connectors/ws"


class FakeWebSocket:
    """Auth-facing face of a Starlette WebSocket."""

    def __init__(self, app: FastAPI, headers: dict[str, str] | None = None) -> None:
        self.app = app
        self.headers = Headers(headers or {})
        self.accepted = False
        self.sent: list[dict] = []
        self.closed: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append(code)

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def receive_json(self) -> dict:
        raise WebSocketDisconnect(code=1000)


async def _insert_connector(
    db: Database, user: CurrentUser, raw_token: str, *, revoked: bool = False
) -> Connector:
    async with db.sessionmaker() as session:
        connector = Connector(
            id=uuid4(),
            tenant_id=user.tenant_id,
            user_id=user.id,
            name="Test laptop",
            token_hash=hash_refresh_token(raw_token),
            revoked_at=datetime.now(tz=UTC) if revoked else None,
        )
        session.add(connector)
        await session.commit()
        return connector


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-real-token"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
    ],
)
async def test_missing_or_invalid_tokens_close_with_4401(
    identity_app: FastAPI, headers: dict[str, str]
) -> None:
    """No usable token never reaches ``accept``; the close code is 4401."""
    socket = FakeWebSocket(identity_app, headers)

    await connector_ws(socket)

    assert socket.accepted is False
    assert socket.closed == [4401]
    assert socket.sent == []


async def test_revoked_tokens_close_with_4401(
    identity_app: FastAPI, seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """A revoked connector token is as unusable as an unknown one."""
    user = seeded_users["viewer@acme.test"]
    await _insert_connector(seeded_db, user, "revoked-token", revoked=True)
    socket = FakeWebSocket(identity_app, {"Authorization": "Bearer revoked-token"})

    await connector_ws(socket)

    assert socket.accepted is False
    assert socket.closed == [4401]


async def test_a_valid_token_is_accepted_registered_and_greeted(
    identity_app: FastAPI, seeded_db: Database, seeded_users: dict[str, CurrentUser]
) -> None:
    """A live token connects: accept, hub registration, ready frame, last seen."""
    user = seeded_users["viewer@acme.test"]
    connector = await _insert_connector(seeded_db, user, "good-token")
    socket = FakeWebSocket(identity_app, {"Authorization": "Bearer good-token"})

    await connector_ws(socket)

    assert socket.accepted is True
    assert socket.sent == [{"type": "ready", "connector_id": str(connector.id)}]
    assert socket.closed == []
    assert identity_app.state.connectors.is_connected(connector.id) is False
    async with seeded_db.sessionmaker() as session:
        stored = (
            await session.execute(select(Connector).where(Connector.id == connector.id))
        ).scalar_one()
        assert stored.last_seen_at is not None
