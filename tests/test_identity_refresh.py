"""Refresh-session API tests: login cookie, rotation, reuse detection, logout.

Schema: these tests rely on the shared ``identity_db`` fixture, which creates
the whole ORM schema with ``Base.metadata.create_all`` (there are no Alembic
revisions yet); the ``RefreshSession`` model lives in
:mod:`app.modules.identity.models`, which the fixture imports, so the table
exists as soon as the model is defined.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.identity.models import RefreshSession, User
from app.modules.identity.security import decode_token

SECRET = "test-secret-0123456789-abcdefghijklmnop"
COOKIE_NAME = "admin-agent.refresh"
COOKIE_PATH = "/api/v1/auth"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"


async def _login(
    client: AsyncClient,
    email: str = "viewer@acme.test",
    password: str = "viewer-pass",
    tenant_slug: str = "acme",
):
    """POST /login and assert success, returning the response."""
    response = await client.post(
        LOGIN,
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 200, response.text
    return response


def _refresh_cookie(response) -> str:
    """The refresh cookie value httpx stored from a login or refresh reply."""
    value = response.cookies.get(COOKIE_NAME)
    assert value, response.headers.get("set-cookie")
    return value


def _use_cookie(client: AsyncClient, value: str) -> None:
    """Replace the client's jar with exactly this refresh cookie (device control)."""
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, value, path=COOKIE_PATH)


async def _refresh_rows(db: Database) -> list[RefreshSession]:
    async with db.sessionmaker() as session:
        result = await session.execute(select(RefreshSession).order_by(RefreshSession.id))
        return list(result.scalars())


async def test_login_sets_an_http_only_scoped_refresh_cookie(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """Login keeps its response shape and adds the refresh cookie plus its row."""
    response = await _login(identity_client, "viewer@acme.test", "viewer-pass")
    body = response.json()
    cookie = _refresh_cookie(response)
    header = response.headers["set-cookie"]

    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60
    assert cookie != body["access_token"]
    assert f"{COOKIE_NAME}={cookie}" in header
    assert "HttpOnly" in header
    assert "SameSite=strict" in header
    assert f"Path={COOKIE_PATH}" in header
    assert f"Max-Age={30 * 24 * 60 * 60}" in header
    assert "Secure" not in header

    rows = await _refresh_rows(seeded_db)
    assert len(rows) == 1
    assert rows[0].user_id == body["user"]["id"]
    assert rows[0].token_hash == hashlib.sha256(cookie.encode()).hexdigest()
    assert rows[0].token_hash != cookie  # only the digest is stored
    assert rows[0].revoked_at is None
    assert rows[0].replaced_by_id is None


async def test_refresh_rotates_the_cookie_and_returns_a_new_access_token(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """A cookie refresh issues a fresh access token and links the rotation."""
    login = await _login(identity_client)
    first = _refresh_cookie(login)

    response = await identity_client.post(REFRESH)
    assert response.status_code == 200, response.text
    body = response.json()
    second = _refresh_cookie(response)

    claims = decode_token(body["access_token"], secret=SECRET)
    assert second != first
    assert claims.sub == str(body["user"]["id"])
    assert claims.role == body["user"]["role"]
    assert claims.tenant_id == body["user"]["tenant_id"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 30 * 60

    rows = await _refresh_rows(seeded_db)
    assert len(rows) == 2
    assert rows[0].family_id == rows[1].family_id
    assert rows[0].revoked_at is not None
    assert rows[0].replaced_by_id == rows[1].id
    assert rows[0].token_hash == hashlib.sha256(first.encode()).hexdigest()
    assert rows[1].token_hash == hashlib.sha256(second.encode()).hexdigest()
    assert rows[1].revoked_at is None

    # The rotated-in cookie still works.
    assert (await identity_client.post(REFRESH)).status_code == 200


async def test_reusing_a_rotated_cookie_revokes_the_whole_family(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """Replaying a rotated token is treated as theft: the newest token dies too."""
    first = _refresh_cookie(await _login(identity_client))
    second = _refresh_cookie(await identity_client.post(REFRESH))
    third = _refresh_cookie(await identity_client.post(REFRESH))

    _use_cookie(identity_client, first)
    reused = await identity_client.post(REFRESH)
    assert reused.status_code == 401
    assert reused.json() == {"detail": "invalid refresh token"}
    assert "Max-Age=0" in reused.headers["set-cookie"]

    _use_cookie(identity_client, third)
    assert (await identity_client.post(REFRESH)).status_code == 401
    _use_cookie(identity_client, second)
    assert (await identity_client.post(REFRESH)).status_code == 401

    rows = await _refresh_rows(seeded_db)
    assert len(rows) == 3
    assert all(row.revoked_at is not None for row in rows)


async def test_expired_refresh_session_is_rejected_and_clears_the_cookie(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """A cookie whose server-side session expired is 401 and gets cleared."""
    await _login(identity_client)
    async with seeded_db.sessionmaker() as session:
        row = (await session.execute(select(RefreshSession))).scalar_one()
        row.expires_at = datetime.now(tz=UTC) - timedelta(minutes=1)
        await session.commit()

    response = await identity_client.post(REFRESH)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid refresh token"}
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_refresh_without_a_cookie_is_401_and_clears_it(
    identity_client: AsyncClient,
) -> None:
    """No cookie at all: uniform 401, no session work, cookie cleared."""
    response = await identity_client.post(REFRESH)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid refresh token"}
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_refresh_with_an_unknown_cookie_is_401(
    identity_client: AsyncClient,
) -> None:
    """A cookie that matches no session row is rejected like an expired one."""
    _use_cookie(identity_client, "not-a-real-session-token")

    response = await identity_client.post(REFRESH)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid refresh token"}


async def test_logout_revokes_only_the_presented_session(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """Logging out one device leaves the other device's session alive."""
    first = _refresh_cookie(await _login(identity_client))
    second = _refresh_cookie(await _login(identity_client))
    assert first != second

    _use_cookie(identity_client, first)
    response = await identity_client.post(LOGOUT)
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]

    _use_cookie(identity_client, first)
    assert (await identity_client.post(REFRESH)).status_code == 401

    _use_cookie(identity_client, second)
    assert (await identity_client.post(REFRESH)).status_code == 200


async def test_logout_without_a_cookie_is_still_204(identity_client: AsyncClient) -> None:
    """Logout is idempotent: no session to revoke is not an error."""
    response = await identity_client.post(LOGOUT)

    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_refresh_keeps_the_sessions_tenant_and_role(
    identity_client: AsyncClient,
) -> None:
    """A refreshed token belongs to the same user, tenant, and role as the session."""
    login = await _login(identity_client, "owner@globex.test", "other-pass", "globex")
    tenant_id = login.json()["user"]["tenant_id"]

    body = (await identity_client.post(REFRESH)).json()
    claims = decode_token(body["access_token"], secret=SECRET)
    assert claims.tenant_id == tenant_id
    assert claims.role == "owner"

    me = await identity_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "owner@globex.test"
    assert me.json()["tenant_id"] == tenant_id


async def test_refresh_is_rejected_for_a_deactivated_user(
    identity_client: AsyncClient, seeded_db: Database
) -> None:
    """A session whose user was deactivated cannot be refreshed."""
    login = await _login(identity_client, "viewer@acme.test", "viewer-pass")
    user_id = login.json()["user"]["id"]
    async with seeded_db.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.is_active = False
        await session.commit()

    assert (await identity_client.post(REFRESH)).status_code == 401


@pytest.fixture
def secure_identity_app(seeded_db: Database) -> FastAPI:
    """Application whose settings demand a Secure refresh cookie (HTTPS)."""
    application = create_app()
    application.state.db = seeded_db
    application.state.settings = Settings(jwt_secret=SECRET, refresh_cookie_secure=True)
    return application


@pytest.fixture
async def secure_identity_client(secure_identity_app: FastAPI) -> AsyncClient:
    """HTTP client bound to the Secure-cookie application."""
    transport = ASGITransport(app=secure_identity_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def test_refresh_cookie_secure_flag_follows_settings(
    secure_identity_client: AsyncClient,
) -> None:
    """``AA_REFRESH_COOKIE_SECURE=true`` adds ``Secure`` to the cookie."""
    response = await _login(secure_identity_client)

    assert "Secure" in response.headers["set-cookie"]
