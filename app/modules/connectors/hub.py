"""In-process hub of connected local connectors (single-worker L1).

Each paired device keeps one outbound WebSocket to the server. The hub owns
those live sockets, routes one request to the most recently connected device
of a user, and resolves responses by request id. There is no Redis and no
cross-worker state: a single worker is the L1 deployment target.

``ConnectorRequester`` is the structural protocol :class:`ToolContext` holds,
so tools never import the WebSocket or database layers.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

#: How long a tool waits for one connector response.
REQUEST_TIMEOUT_SECONDS = 30.0


class ConnectorHubError(Exception):
    """Base class for a connector that could not answer a request."""


class ConnectorOfflineError(ConnectorHubError):
    """The user has no online connector, or its socket went away."""


class ConnectorTimeoutError(ConnectorHubError):
    """The connector did not answer before the timeout."""


class ConnectorResponseError(ConnectorHubError):
    """The connector answered ``ok: false`` with its own error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConnectorRequester(Protocol):
    """What folder tools need from the hub (no circular imports)."""

    def is_online(self, user_id: int) -> bool:
        """True when ``user_id`` has at least one connected device."""
        ...

    async def request(
        self,
        user_id: int,
        op: str,
        args: dict[str, Any],
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Send one operation to the user's newest online connector."""
        ...


@dataclass
class _Connection:
    """One accepted WebSocket and the principal/user it serves."""

    connector_id: UUID
    user_id: int
    websocket: Any
    sequence: int


@dataclass
class _Pending:
    """One in-flight request whose response has not arrived yet."""

    connector_id: UUID
    future: asyncio.Future[dict[str, Any]]


class ConnectorHub:
    """Live connector registry plus request/response correlation."""

    def __init__(self) -> None:
        self._connections: dict[UUID, _Connection] = {}
        self._pending: dict[str, _Pending] = {}
        self._sequence = 0

    async def register(self, connector_id: UUID, user_id: int, websocket: Any) -> None:
        """Register a freshly accepted socket, replacing a stale one if any."""
        previous = self._connections.get(connector_id)
        if previous is not None:
            self._fail_pending(connector_id, ConnectorOfflineError("connector reconnected"))
            await self._close(previous.websocket)
        self._sequence += 1
        self._connections[connector_id] = _Connection(
            connector_id=connector_id,
            user_id=user_id,
            websocket=websocket,
            sequence=self._sequence,
        )

    def unregister(self, connector_id: UUID, websocket: Any | None = None) -> bool:
        """Forget a socket (only if it is still the registered one) and fail its work."""
        connection = self._connections.get(connector_id)
        if connection is None:
            return False
        if websocket is not None and connection.websocket is not websocket:
            return False
        del self._connections[connector_id]
        self._fail_pending(connector_id, ConnectorOfflineError("connector disconnected"))
        return True

    def is_online(self, user_id: int) -> bool:
        """True while at least one connector of ``user_id`` is connected."""
        return any(connection.user_id == user_id for connection in self._connections.values())

    def is_connected(self, connector_id: UUID) -> bool:
        """True while this exact connector has a live socket."""
        return connector_id in self._connections

    def resolve(self, connector_id: UUID, message: Any) -> bool:
        """Deliver one connector response to its pending request.

        Unknown ids, malformed frames, and responses from another connector are
        ignored (return ``False``) instead of raising.
        """
        if not isinstance(message, dict) or message.get("type") != "response":
            return False
        request_id = message.get("id")
        if not isinstance(request_id, str):
            return False
        pending = self._pending.get(request_id)
        if pending is None or pending.connector_id != connector_id:
            return False
        future = pending.future
        if future.done():
            return True
        if message.get("ok") is True:
            result = message.get("result")
            if isinstance(result, dict):
                future.set_result(result)
            else:
                future.set_exception(
                    ConnectorResponseError("invalid_response", "result must be an object")
                )
            return True
        error = message.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        text = error.get("message") if isinstance(error, dict) else None
        future.set_exception(
            ConnectorResponseError(str(code or "connector_error"), str(text or ""))
        )
        return True

    async def request(
        self,
        user_id: int,
        op: str,
        args: dict[str, Any],
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Send ``op`` to the user's newest online connector and await its result.

        Raises:
            ConnectorOfflineError: no device is online or the send failed.
            ConnectorTimeoutError: no response arrived within ``timeout``.
            ConnectorResponseError: the connector answered ``ok: false``.
        """
        connection = self._latest(user_id)
        if connection is None:
            raise ConnectorOfflineError("no connector is online for this user")

        request_id = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = _Pending(connection.connector_id, future)
        try:
            await connection.websocket.send_json(
                {"type": "request", "id": request_id, "op": op, "args": args}
            )
        except Exception as exc:
            self._pending.pop(request_id, None)
            self.unregister(connection.connector_id, connection.websocket)
            raise ConnectorOfflineError("could not send to the connector") from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise ConnectorTimeoutError("connector did not answer in time") from exc
        finally:
            self._pending.pop(request_id, None)

    async def disconnect(self, connector_id: UUID) -> bool:
        """Close this connector's socket, if connected, and fail pending work."""
        connection = self._connections.pop(connector_id, None)
        if connection is None:
            return False
        self._fail_pending(connector_id, ConnectorOfflineError("connector disconnected"))
        await self._close(connection.websocket)
        return True

    async def close_all(self) -> None:
        """Close every live socket (application shutdown)."""
        connections = list(self._connections.values())
        self._connections.clear()
        for connection in connections:
            await self._close(connection.websocket)
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(ConnectorOfflineError("server shutting down"))
        self._pending.clear()

    def _latest(self, user_id: int) -> _Connection | None:
        candidates = [
            connection
            for connection in self._connections.values()
            if connection.user_id == user_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda connection: connection.sequence)

    def _fail_pending(self, connector_id: UUID, error: ConnectorHubError) -> None:
        for request_id, pending in list(self._pending.items()):
            if pending.connector_id != connector_id:
                continue
            del self._pending[request_id]
            if not pending.future.done():
                pending.future.set_exception(error)

    @staticmethod
    async def _close(websocket: Any) -> None:
        try:
            await websocket.close(code=1000)
        except Exception:  # pragma: no cover - a dead socket is already closed
            pass
