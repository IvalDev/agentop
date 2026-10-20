"""Connector service: pairing codes, token authentication, listing, revoke.

Pairing is the only unauthenticated flow in this module: an eight-character
code generated for one user is the secret the local connector redeems. Codes
are single-use, expire after ten minutes, and only their SHA-256 digest is
stored. Connector bearer tokens follow the same rule as refresh sessions: the
raw token is returned once, the database keeps its digest.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.models import Connector, ConnectorPairingCode
from app.modules.identity.deps import CurrentUser

#: Pairing-code alphabet: no I/O/0/1, so the code is unambiguous when typed.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
PAIRING_CODE_TTL_MINUTES = 10
#: URL-safe entropy of a connector token (32 bytes → 43 characters).
CONNECTOR_TOKEN_BYTES = 32


class ConnectorsError(Exception):
    """Base class for connector-service refusals."""


class PairingCodeInvalidError(ConnectorsError):
    """The code is unknown, expired, already used, or invalidated (one 400)."""


class ConnectorNotFoundError(ConnectorsError):
    """No active connector with that id belongs to the caller."""


def hash_secret(raw: str) -> str:
    """SHA-256 hex digest stored for tokens and pairing codes."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_pairing_code(code: str) -> str:
    """Canonical form of a code: uppercase with hyphens removed."""
    return code.strip().upper().replace("-", "")


def format_pairing_code(normalized: str) -> str:
    """Display form ``XXXX-XXXX`` of an eight-character code."""
    return f"{normalized[:4]}-{normalized[4:]}"


def _as_utc(value: datetime) -> datetime:
    """Re-attach UTC to timestamps SQLite returns naive (Postgres keeps them aware)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def create_pairing_code(
    db: AsyncSession, user: CurrentUser
) -> tuple[ConnectorPairingCode, str]:
    """Mint a code for ``user``, invalidating their previous unused one.

    Returns the stored row and the formatted code (the raw code is never
    persisted).
    """
    now = _now()
    await db.execute(
        update(ConnectorPairingCode)
        .where(
            ConnectorPairingCode.user_id == user.id,
            ConnectorPairingCode.used_at.is_(None),
            ConnectorPairingCode.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    raw_code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    row = ConnectorPairingCode(
        tenant_id=user.tenant_id,
        user_id=user.id,
        code_hash=hash_secret(raw_code),
        expires_at=now + timedelta(minutes=PAIRING_CODE_TTL_MINUTES),
    )
    db.add(row)
    await db.flush()
    return row, format_pairing_code(raw_code)


async def pair_connector(
    db: AsyncSession, code: str, name: str
) -> tuple[Connector, str]:
    """Redeem ``code`` into a connector for its owner; return the raw token once.

    Raises:
        PairingCodeInvalidError: the code is unknown, expired, used, or
            invalidated. Callers answer the same 400 for every case.
    """
    normalized = normalize_pairing_code(code)
    result = await db.execute(
        select(ConnectorPairingCode).where(
            ConnectorPairingCode.code_hash == hash_secret(normalized)
        )
    )
    pairing = result.scalar_one_or_none()
    now = _now()
    if (
        pairing is None
        or pairing.used_at is not None
        or pairing.revoked_at is not None
        or _as_utc(pairing.expires_at) <= now
    ):
        raise PairingCodeInvalidError("invalid or expired pairing code")

    raw_token = secrets.token_urlsafe(CONNECTOR_TOKEN_BYTES)
    connector = Connector(
        tenant_id=pairing.tenant_id,
        user_id=pairing.user_id,
        name=name.strip(),
        token_hash=hash_secret(raw_token),
    )
    pairing.used_at = now
    db.add(connector)
    await db.flush()
    return connector, raw_token


async def list_for_user(
    db: AsyncSession, *, tenant_id: int, user_id: int
) -> list[Connector]:
    """The caller's own active connectors, newest first."""
    result = await db.execute(
        select(Connector)
        .where(
            Connector.tenant_id == tenant_id,
            Connector.user_id == user_id,
            Connector.revoked_at.is_(None),
        )
        .order_by(Connector.created_at.desc())
    )
    return list(result.scalars())


async def revoke_connector(
    db: AsyncSession, *, tenant_id: int, user_id: int, connector_id: UUID
) -> Connector:
    """Mark one of the caller's connectors revoked.

    Raises:
        ConnectorNotFoundError: unknown, foreign, or already revoked (one 404).
    """
    result = await db.execute(
        select(Connector).where(
            Connector.id == connector_id,
            Connector.tenant_id == tenant_id,
            Connector.user_id == user_id,
            Connector.revoked_at.is_(None),
        )
    )
    connector = result.scalar_one_or_none()
    if connector is None:
        raise ConnectorNotFoundError("connector not found")
    connector.revoked_at = _now()
    await db.flush()
    return connector


async def authenticate(db: AsyncSession, raw_token: str) -> Connector | None:
    """Return the active connector for a raw bearer token, else ``None``."""
    if not raw_token:
        return None
    result = await db.execute(
        select(Connector).where(Connector.token_hash == hash_secret(raw_token))
    )
    connector = result.scalar_one_or_none()
    if connector is None or connector.revoked_at is not None:
        return None
    return connector


async def mark_seen(db: AsyncSession, connector: Connector) -> None:
    """Record that this connector just connected or disconnected."""
    connector.last_seen_at = _now()
    await db.flush()
