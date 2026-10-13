"""Password hashing (bcrypt) and local JWT issuing/validation (PyJWT)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TOKEN_ISSUER = "admin-agent"
TOKEN_AUDIENCE = "admin-agent"


class TokenError(Exception):
    """Raised when a JWT is malformed, expired, or fails validation."""


@dataclass(frozen=True)
class TokenClaims:
    """Validated JWT claims for an access token."""

    sub: str
    tenant_id: int
    role: str
    exp: datetime
    iat: datetime
    iss: str
    aud: str


def hash_password(password: str) -> str:
    """Return a bcrypt hash of ``password``; never log or return the input."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return ``True`` when ``plain_password`` matches the stored hash.

    Malformed hashes (e.g. legacy data) count as non-matches instead of
    raising, so callers can treat every failure uniformly.
    """
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        return False


def create_access_token(
    sub: str,
    tenant_id: int,
    role: str,
    expires_minutes: int,
    *,
    secret: str,
    algorithm: str = "HS256",
) -> str:
    """Issue a signed access token with the standard admin-agent claims."""
    now = datetime.now(tz=UTC)
    payload = {
        "sub": str(sub),
        "tenant_id": tenant_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
    }
    return pyjwt.encode(payload, secret, algorithm=algorithm)


def decode_token(token: str, *, secret: str, algorithm: str = "HS256") -> TokenClaims:
    """Validate ``token`` and return its claims.

    Raises:
        TokenError: on any validation failure (signature, expiry, issuer,
            audience, or missing required claims).
    """
    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            issuer=TOKEN_ISSUER,
            audience=TOKEN_AUDIENCE,
        )
    except pyjwt.PyJWTError as exc:
        raise TokenError("invalid token") from exc
    try:
        return TokenClaims(
            sub=str(payload["sub"]),
            tenant_id=int(payload["tenant_id"]),
            role=str(payload["role"]),
            exp=datetime.fromtimestamp(payload["exp"], tz=UTC),
            iat=datetime.fromtimestamp(payload["iat"], tz=UTC),
            iss=str(payload["iss"]),
            aud=str(payload["aud"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("invalid token claims") from exc
