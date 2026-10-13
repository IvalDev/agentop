"""Shared request/response schemas; populated as feature tasks land."""

from datetime import datetime

from pydantic import BaseModel


class LoginRequest(BaseModel):
    """Credentials for ``POST /api/v1/auth/login``."""

    email: str
    password: str
    tenant_slug: str


class UserPayload(BaseModel):
    """Public user fields safe to return from auth endpoints."""

    id: int
    email: str
    role: str
    tenant_id: int


class LoginResponse(BaseModel):
    """Issued access token plus the authenticated user's public profile."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserPayload


class MeResponse(BaseModel):
    """Profile returned by ``GET /api/v1/auth/me``."""

    id: int
    email: str
    full_name: str
    role: str
    tenant_id: int


class TokenIntrospectResponse(BaseModel):
    """Validated JWT claims returned by ``POST /api/v1/auth/token/introspect``."""

    sub: str
    tenant_id: int
    role: str
    exp: datetime
    iat: datetime
    iss: str
    aud: str


class AdminProbeResponse(BaseModel):
    """Response of the admin+ role-gate probe endpoint."""

    status: str
