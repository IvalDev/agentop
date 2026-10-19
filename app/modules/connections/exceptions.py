"""Exceptions raised by the per-user connections service.

Every error maps to exactly one HTTP answer in the router
(:data:`~app.modules.connections.router._HTTP_STATUS`), and none of the
messages carry secrets — the router serves fixed detail strings, so the
response can never echo credentials, ciphertext, or keys. All three share
:class:`ConnectionsError` for handlers that want one catch clause.
"""


class ConnectionsError(Exception):
    """Base class for every connections-service error."""


class ConnectionNotFoundError(ConnectionsError):
    """No such connection in the caller's scope (router: 404).

    One message for unknown ids, other tenants' ids, and other users' ids
    alike, so a request can never learn whether a foreign connection exists.
    """


class ConnectionConflictError(ConnectionsError):
    """An admin replace would collide with another row's natural key (router: 409)."""


class CredentialsEncryptionError(ConnectionsError):
    """Encryption cannot run: no key configured or the key is malformed (router: 500).

    Raised *before* any write — the fail-closed rule: without a usable key
    the connection is never stored, in plaintext or otherwise.
    """
