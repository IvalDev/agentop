"""Connector API: pairing codes, device management, and the WebSocket.

REST (all under ``/api/v1/connectors``):

========================================== ========================== ==================
route                                      meaning                    access
========================================== ========================== ==================
``POST /pairing-codes``                     mint an 8-char code        any authenticated
``POST /pair``                              redeem a code for a token  none (code is secret)
``GET /``                                   own connectors + live state any authenticated
``DELETE /{id}``                            revoke + disconnect        owner only
========================================== ========================== ==================

The WebSocket at ``/ws`` is authenticated with ``Authorization: Bearer
<connector token>`` parsed from the handshake headers (never cookies, never
an HTTP auth dependency). The route opens its own database session from the
app's session factory because :func:`app.core.db.get_session` needs an HTTP
``Request``. Missing, unknown, or revoked tokens close with ``4401``; accepted
sockets get ``{"type": "ready", "connector_id": ...}`` and answer the hub's
requests. The server sends ``{"type": "ping"}`` every 30 seconds.
"""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.core.db import Database, get_session
from app.modules.connectors.hub import ConnectorHub
from app.modules.connectors.models import Connector
from app.modules.connectors.schemas import (
    ConnectorListResponse,
    ConnectorResponse,
    PairingCodeResponse,
    PairRequest,
    PairResponse,
)
from app.modules.connectors.service import (
    ConnectorNotFoundError,
    PairingCodeInvalidError,
    authenticate,
    create_pairing_code,
    list_for_user,
    mark_seen,
    pair_connector,
    revoke_connector,
)
from app.modules.identity.deps import CurrentUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]

INVALID_PAIRING_CODE = "Código inválido o vencido"
_CONNECTOR_NOT_FOUND = "connector not found"
#: Application-level liveness ping of an accepted connector.
PING_INTERVAL_SECONDS = 30.0


def get_connector_hub(request: Request) -> ConnectorHub:
    """The process-wide connector hub attached to ``app.state``."""
    return request.app.state.connectors


HubDep = Annotated[ConnectorHub, Depends(get_connector_hub)]


def _utc(value: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to UTC (SQLite returns naive values)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _response(connector: Connector, connected: bool) -> ConnectorResponse:
    """Public view of a connector: no token, hash, tenant, or folder path."""
    return ConnectorResponse(
        id=connector.id,
        name=connector.name,
        connected=connected,
        last_seen_at=_utc(connector.last_seen_at),
        created_at=_utc(connector.created_at),
    )


@router.post("/pairing-codes", response_model=PairingCodeResponse, status_code=201)
async def create_code(session: SessionDep, user: CurrentUserDep) -> PairingCodeResponse:
    """Mint a ten-minute pairing code, invalidating the user's previous one."""
    row, code = await create_pairing_code(session, user)
    await session.commit()
    return PairingCodeResponse(code=code, expires_at=_utc(row.expires_at))


@router.post("/pair", response_model=PairResponse, status_code=201)
async def pair(body: PairRequest, session: SessionDep) -> PairResponse:
    """Redeem a pairing code; no session is needed because the code is the secret."""
    try:
        connector, token = await pair_connector(session, body.code, body.name)
    except PairingCodeInvalidError as exc:
        raise HTTPException(status_code=400, detail=INVALID_PAIRING_CODE) from exc
    await session.commit()
    return PairResponse(connector_id=connector.id, token=token, name=connector.name)


@router.get("", response_model=ConnectorListResponse)
async def list_connectors(
    session: SessionDep, user: CurrentUserDep, hub: HubDep
) -> ConnectorListResponse:
    """The caller's own active connectors, with live connection state from the hub."""
    rows = await list_for_user(session, tenant_id=user.tenant_id, user_id=user.id)
    return ConnectorListResponse(
        connectors=[_response(row, hub.is_connected(row.id)) for row in rows]
    )


@router.delete("/{connector_id}", status_code=204)
async def revoke(
    connector_id: UUID, session: SessionDep, user: CurrentUserDep, hub: HubDep
) -> None:
    """Revoke one of the caller's connectors and close its live WebSocket."""
    try:
        connector = await revoke_connector(
            session,
            tenant_id=user.tenant_id,
            user_id=user.id,
            connector_id=connector_id,
        )
    except ConnectorNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_CONNECTOR_NOT_FOUND) from exc
    await session.commit()
    await hub.disconnect(connector.id)


def _bearer_token(header: str | None) -> str | None:
    """Raw token of ``Authorization: Bearer <token>``, or ``None``."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def _ping_loop(websocket: WebSocket) -> None:
    """Application-level liveness: one ping every 30 seconds until cancelled."""
    while True:
        await asyncio.sleep(PING_INTERVAL_SECONDS)
        try:
            await websocket.send_json({"type": "ping"})
        except Exception:
            return


async def _touch_last_seen(db: Database, connector_id: UUID) -> None:
    """Best-effort ``last_seen_at`` update on disconnect."""
    try:
        async with db.sessionmaker() as session:
            connector = await session.get(Connector, connector_id)
            if connector is None:
                return
            await mark_seen(session, connector)
            await session.commit()
    except Exception:
        logger.warning("could not update connector last_seen_at", exc_info=True)


@router.websocket("/ws")
async def connector_ws(websocket: WebSocket) -> None:
    """Accept one authenticated connector and serve its request/response traffic."""
    token = _bearer_token(websocket.headers.get("authorization"))
    db: Database | None = getattr(websocket.app.state, "db", None)
    hub: ConnectorHub | None = getattr(websocket.app.state, "connectors", None)
    if token is None:
        await websocket.close(code=4401)
        return
    if db is None or hub is None:
        logger.error("connector WebSocket opened without a database or hub")
        await websocket.close(code=1011)
        return

    async with db.sessionmaker() as session:
        connector = await authenticate(session, token)
        if connector is None:
            await websocket.close(code=4401)
            return
        connector_id = connector.id
        user_id = connector.user_id
        await mark_seen(session, connector)
        await session.commit()

    await websocket.accept()
    await hub.register(connector_id, user_id, websocket)
    await websocket.send_json({"type": "ready", "connector_id": str(connector_id)})

    ping = asyncio.create_task(_ping_loop(websocket))
    try:
        while True:
            message = await websocket.receive_json()
            hub.resolve(connector_id, message)
    except WebSocketDisconnect:
        pass
    finally:
        await _cancel(ping)
        hub.unregister(connector_id, websocket)
        await _touch_last_seen(db, connector_id)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
