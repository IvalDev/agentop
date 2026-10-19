"""Auth API tests: login, token introspection, /me, and RBAC probe."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.modules.identity.security import create_access_token, decode_token


async def _login(client: AsyncClient, email: str, password: str, tenant_slug: str = "acme") -> dict:
    """POST /login and assert success, returning the response body."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_login_success_returns_decodable_token(identity_client: AsyncClient) -> None:
    """Login succeeds and the token carries the user's tenant and role."""
    body = await _login(identity_client, "viewer@acme.test", "viewer-pass")
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60
    assert body["user"]["email"] == "viewer@acme.test"
    assert body["user"]["role"] == "viewer"
    claims = decode_token(body["access_token"], secret="test-secret-0123456789-abcdefghijklmnop")
    assert claims.sub == str(body["user"]["id"])
    assert claims.role == "viewer"
    assert claims.tenant_id == body["user"]["tenant_id"]


@pytest.mark.parametrize(
    ("email", "password", "tenant_slug"),
    [
        ("viewer@acme.test", "wrong-pass", "acme"),
        ("missing@acme.test", "viewer-pass", "acme"),
        ("viewer@acme.test", "viewer-pass", "unknown-tenant"),
    ],
)
async def test_login_failures_are_uniform_401(
    identity_client: AsyncClient, email: str, password: str, tenant_slug: str
) -> None:
    """Wrong password, unknown user, and unknown tenant share one error shape."""
    response = await identity_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


async def test_me_requires_authentication(identity_client: AsyncClient) -> None:
    """GET /me without a token is 401."""
    response = await identity_client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_me_returns_current_user(identity_client: AsyncClient) -> None:
    """GET /me with a valid token returns the authenticated user."""
    body = await _login(identity_client, "owner@acme.test", "owner-pass")
    response = await identity_client.get("/api/v1/auth/me", headers=_bearer(body["access_token"]))
    assert response.status_code == 200
    me = response.json()
    assert me["email"] == "owner@acme.test"
    assert me["role"] == "owner"
    assert me["tenant_id"] == body["user"]["tenant_id"]


async def test_me_rejects_expired_token(identity_client: AsyncClient) -> None:
    """GET /me with an expired token is 401."""
    body = await _login(identity_client, "owner@acme.test", "owner-pass")
    expired = create_access_token(
        sub=str(body["user"]["id"]),
        tenant_id=body["user"]["tenant_id"],
        role=body["user"]["role"],
        expires_minutes=-1,
        secret="test-secret-0123456789-abcdefghijklmnop",
    )
    response = await identity_client.get("/api/v1/auth/me", headers=_bearer(expired))
    assert response.status_code == 401


async def test_me_rejects_token_signed_with_other_secret(
    identity_client: AsyncClient,
) -> None:
    """GET /me with a forged token is 401."""
    forged = create_access_token(
        sub="1",
        tenant_id=1,
        role="owner",
        expires_minutes=5,
        secret="attacker-secret-0123456789-abcdefgh",
    )
    response = await identity_client.get("/api/v1/auth/me", headers=_bearer(forged))
    assert response.status_code == 401


async def test_introspect_returns_claims(identity_client: AsyncClient) -> None:
    """POST /token/introspect echoes the decoded claims for the caller."""
    body = await _login(identity_client, "admin@acme.test", "admin-pass")
    response = await identity_client.post(
        "/api/v1/auth/token/introspect", headers=_bearer(body["access_token"])
    )
    assert response.status_code == 200
    claims = response.json()
    assert claims["role"] == "admin"
    assert claims["tenant_id"] == body["user"]["tenant_id"]
    assert claims["iss"] == "admin-agent"
    assert claims["aud"] == "admin-agent"
    assert datetime.fromisoformat(claims["exp"]) > datetime.now(tz=UTC)


async def test_introspect_requires_authentication(identity_client: AsyncClient) -> None:
    """POST /token/introspect without a token is 401."""
    response = await identity_client.post("/api/v1/auth/token/introspect")
    assert response.status_code == 401


async def test_admin_probe_allows_admin(identity_client: AsyncClient) -> None:
    """Admin users pass the admin+ role gate."""
    body = await _login(identity_client, "admin@acme.test", "admin-pass")
    response = await identity_client.get(
        "/api/v1/auth/admin-probe", headers=_bearer(body["access_token"])
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_admin_probe_allows_owner(identity_client: AsyncClient) -> None:
    """Owner users pass the admin+ role gate."""
    body = await _login(identity_client, "owner@acme.test", "owner-pass")
    response = await identity_client.get(
        "/api/v1/auth/admin-probe", headers=_bearer(body["access_token"])
    )
    assert response.status_code == 200


async def test_admin_probe_forbids_viewer(identity_client: AsyncClient) -> None:
    """Viewer role gets 403 on the admin+ probe endpoint."""
    body = await _login(identity_client, "viewer@acme.test", "viewer-pass")
    response = await identity_client.get(
        "/api/v1/auth/admin-probe", headers=_bearer(body["access_token"])
    )
    assert response.status_code == 403
