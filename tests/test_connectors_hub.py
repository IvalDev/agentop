"""In-process connector hub tests: registration, request/response, failures.

The hub is the server side of one WebSocket per connector: it routes one
request at a time to the most recently connected device of the user, resolves
responses by id, fails pending work when the device goes away, and enforces a
30-second default timeout. A fake WebSocket stands in for the transport; the
WebSocket route itself is covered separately in ``test_connectors_ws``.
"""

import asyncio
from uuid import uuid4

import pytest

from app.modules.connectors.hub import (
    ConnectorHub,
    ConnectorOfflineError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)


class FakeWebSocket:
    """Records sent frames and can answer requests from a scripted responder."""

    def __init__(self, hub: ConnectorHub | None = None, responder=None) -> None:
        self.hub = hub
        self.responder = responder
        self.sent: list[dict] = []
        self.closed: list[int] = []
        self.connector_id = None

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        if self.hub is not None and self.connector_id is not None and self.responder is not None:
            if message.get("type") == "request":
                self.hub.resolve(self.connector_id, self.responder(message))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append(code)


def answering(result: dict):
    """Responder that always succeeds with ``result``."""

    def respond(message: dict) -> dict:
        return {"type": "response", "id": message["id"], "ok": True, "result": result}

    return respond


async def test_request_round_trip_sends_the_operation_and_returns_the_result() -> None:
    """A request carries the op and args; the response resolves the caller."""
    hub = ConnectorHub()
    connector_id, user_id = uuid4(), 7
    socket = FakeWebSocket(hub, answering({"path": "", "entries": []}))
    socket.connector_id = connector_id
    await hub.register(connector_id, user_id, socket)

    result = await hub.request(user_id, "list_files", {"path": ""}, timeout=1)

    assert result == {"path": "", "entries": []}
    [request] = socket.sent
    assert request["type"] == "request"
    assert request["op"] == "list_files"
    assert request["args"] == {"path": ""}
    assert request["id"]


async def test_request_prefers_the_most_recently_connected_device() -> None:
    """The hub picks the newest online connector of the user."""
    hub = ConnectorHub()
    user_id = 7
    older_id, newer_id = uuid4(), uuid4()
    older = FakeWebSocket(hub, answering({"device": "older"}))
    newer = FakeWebSocket(hub, answering({"device": "newer"}))
    older.connector_id, newer.connector_id = older_id, newer_id
    await hub.register(older_id, user_id, older)
    await hub.register(newer_id, user_id, newer)

    assert (await hub.request(user_id, "list_files", {}, timeout=1))["device"] == "newer"
    assert older.sent == []

    hub.unregister(newer_id, newer)
    assert (await hub.request(user_id, "list_files", {}, timeout=1))["device"] == "older"


async def test_request_without_an_online_connector_fails_fast() -> None:
    """No device for the user is an offline error, never a hung request."""
    hub = ConnectorHub()

    with pytest.raises(ConnectorOfflineError):
        await hub.request(7, "list_files", {}, timeout=1)


async def test_request_times_out_when_the_connector_does_not_answer() -> None:
    """An accepted request that never gets a response times out."""
    hub = ConnectorHub()
    connector_id, user_id = uuid4(), 7
    socket = FakeWebSocket()
    await hub.register(connector_id, user_id, socket)

    with pytest.raises(ConnectorTimeoutError):
        await hub.request(user_id, "read_file", {"path": "a.txt"}, timeout=0.01)

    [request] = socket.sent
    assert request["op"] == "read_file"


async def test_a_connector_error_response_raises_a_typed_error() -> None:
    """``ok: false`` becomes a ``ConnectorResponseError`` with its code and message."""
    hub = ConnectorHub()
    connector_id, user_id = uuid4(), 7

    def respond(message: dict) -> dict:
        return {
            "type": "response",
            "id": message["id"],
            "ok": False,
            "error": {"code": "not_found", "message": "missing"},
        }

    socket = FakeWebSocket(hub, respond)
    socket.connector_id = connector_id
    await hub.register(connector_id, user_id, socket)

    with pytest.raises(ConnectorResponseError) as exc:
        await hub.request(user_id, "read_file", {"path": "missing.txt"}, timeout=1)

    assert (exc.value.code, exc.value.message) == ("not_found", "missing")


async def test_disconnecting_fails_pending_requests() -> None:
    """A device that disconnects mid-request unblocks the caller as offline."""
    hub = ConnectorHub()
    connector_id, user_id = uuid4(), 7
    socket = FakeWebSocket()
    await hub.register(connector_id, user_id, socket)

    task = asyncio.create_task(hub.request(user_id, "read_file", {"path": "a.txt"}, timeout=30))
    for _ in range(100):
        if socket.sent:
            break
        await asyncio.sleep(0)

    await hub.disconnect(connector_id)

    with pytest.raises(ConnectorOfflineError):
        await task
    assert socket.closed == [1000]
    assert hub.is_connected(connector_id) is False


async def test_reconnecting_the_same_connector_replaces_and_closes_the_old_socket() -> None:
    """A duplicate registration wins; the stale socket is closed."""
    hub = ConnectorHub()
    connector_id, user_id = uuid4(), 7
    old = FakeWebSocket()
    new = FakeWebSocket(hub, answering({"ok": True}))
    new.connector_id = connector_id

    await hub.register(connector_id, user_id, old)
    await hub.register(connector_id, user_id, new)

    assert old.closed == [1000]
    assert await hub.request(user_id, "list_files", {}, timeout=1) == {"ok": True}


async def test_online_checks_and_close_all() -> None:
    """The hub reports per-user/per-connector state and closes everything on shutdown."""
    hub = ConnectorHub()
    user_id = 7
    first_id, second_id = uuid4(), uuid4()
    first, second = FakeWebSocket(), FakeWebSocket()
    await hub.register(first_id, user_id, first)
    await hub.register(second_id, user_id, second)

    assert hub.is_online(user_id) is True
    assert hub.is_online(999) is False
    assert hub.is_connected(first_id) is True

    await hub.close_all()

    assert first.closed == second.closed == [1000]
    assert hub.is_online(user_id) is False
    assert hub.is_connected(first_id) is False


async def test_responses_for_unknown_or_mismatched_requests_are_ignored() -> None:
    """A late or foreign response never resolves another connector's request."""
    hub = ConnectorHub()
    connector_id, other_id, user_id = uuid4(), uuid4(), 7
    socket = FakeWebSocket()
    await hub.register(connector_id, user_id, socket)

    assert hub.resolve(connector_id, {"type": "response", "id": "ghost", "ok": True}) is False
    assert hub.resolve(other_id, {"type": "response", "id": "ghost", "ok": True}) is False
    assert hub.resolve(connector_id, {"type": "pong"}) is False
