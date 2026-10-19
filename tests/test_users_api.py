"""Team management API tests: roles, guards, tenant isolation, and audit."""

import json
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from passlib.context import CryptContext
from sqlalchemy import func, select

from app.core.db import Database
from app.modules.identity import security
from app.modules.identity.models import Tenant, User, UserRole
from app.modules.identity.security import create_access_token

USERS = "/api/v1/users"
SECRET = "test-secret-0123456789-abcdefghijklmnop"
#: Hash that never verifies: these extra members only use minted tokens.
UNUSABLE_HASH = "!"

NEW_USER = {
    "email": "new.hire@acme.test",
    "full_name": "New Hire",
    "role": "operator",
    "password": "first-day-pass",
}
#: Every route of the API as (method, path suffix, JSON body).
ENDPOINTS = [
    ("GET", "", None),
    ("POST", "", NEW_USER),
    ("PUT", "/{id}/role", {"role": "operator"}),
    ("POST", "/{id}/deactivate", None),
    ("POST", "/{id}/reactivate", None),
]
#: The routes that change one existing user.
MUTATIONS = ENDPOINTS[2:]


@dataclass(frozen=True)
class Team:
    """Seeded members of both tenants plus helpers to act as any of them."""

    db: Database
    members: dict[str, tuple[int, int, str]]

    def id(self, email: str) -> int:
        return self.members[email][0]

    def auth(self, email: str) -> dict[str, str]:
        user_id, tenant_id, role = self.members[email]
        token = create_access_token(
            sub=str(user_id), tenant_id=tenant_id, role=role, expires_minutes=5, secret=SECRET
        )
        return {"Authorization": f"Bearer {token}"}

    async def row(self, user_id: int) -> User:
        async with self.db.sessionmaker() as session:
            return await session.get(User, user_id)


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimum bcrypt cost: every test here seeds and creates hashed users."""
    monkeypatch.setattr(security, "pwd_context", CryptContext(schemes=["bcrypt"], bcrypt__rounds=4))


@pytest.fixture
async def team(seeded_db: Database) -> Team:
    """``seeded_db`` plus an Acme operator, an inactive owner, and an in-tenant superadmin."""
    async with seeded_db.sessionmaker() as session:
        acme = (await session.execute(select(Tenant).where(Tenant.slug == "acme"))).scalar_one()
        session.add_all(
            [
                User(
                    tenant_id=acme.id,
                    email="operator@acme.test",
                    full_name="Acme Operator",
                    hashed_password=UNUSABLE_HASH,
                    role=UserRole.OPERATOR,
                ),
                User(
                    tenant_id=acme.id,
                    email="dormant@acme.test",
                    full_name="Dormant Owner",
                    hashed_password=UNUSABLE_HASH,
                    role=UserRole.OWNER,
                    is_active=False,
                ),
                User(
                    tenant_id=acme.id,
                    email="root@acme.test",
                    full_name="Platform Operator",
                    hashed_password=UNUSABLE_HASH,
                    role=UserRole.ADMIN,
                    is_superadmin=True,
                ),
            ]
        )
        await session.commit()
        users = (await session.execute(select(User))).scalars().all()
    return Team(seeded_db, {u.email: (u.id, u.tenant_id, u.role.value) for u in users})


async def _call(
    client: AsyncClient,
    team: Team,
    caller: str,
    method: str,
    suffix: str = "",
    body: dict | None = None,
    target: str | int = "viewer@acme.test",
):
    """Send one request to the users API as ``caller`` against ``target``."""
    target_id = target if isinstance(target, int) else team.id(target)
    return await client.request(
        method, USERS + suffix.format(id=target_id), json=body, headers=team.auth(caller)
    )


async def _login(client: AsyncClient, email: str, password: str):
    return await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": "acme"},
    )


# --- authorization ---------------------------------------------------------


async def test_users_api_requires_authentication(identity_client: AsyncClient) -> None:
    """Without a token the API answers 401."""
    assert (await identity_client.get(USERS)).status_code == 401


@pytest.mark.parametrize("caller", ["viewer@acme.test", "operator@acme.test"])
@pytest.mark.parametrize(("method", "suffix", "body"), ENDPOINTS)
async def test_users_api_forbids_operator_and_viewer(
    identity_client: AsyncClient, team: Team, caller: str, method: str, suffix: str, body
) -> None:
    """Every route is owner/admin only; lower roles change nothing."""
    response = await _call(
        identity_client, team, caller, method, suffix, body, target="admin@acme.test"
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    admin = await team.row(team.id("admin@acme.test"))
    assert (admin.role, admin.is_active) == (UserRole.ADMIN, True)


@pytest.mark.parametrize("caller", ["owner@acme.test", "admin@acme.test"])
async def test_list_returns_only_the_callers_tenant(
    identity_client: AsyncClient, team: Team, caller: str
) -> None:
    """The listing holds the caller's tenant only, with public fields only."""
    response = await _call(identity_client, team, caller, "GET")
    assert response.status_code == 200
    users = response.json()["users"]
    assert [u["email"] for u in users] == [
        "owner@acme.test",
        "admin@acme.test",
        "viewer@acme.test",
        "operator@acme.test",
        "dormant@acme.test",
        "root@acme.test",
    ]
    for user in users:
        assert set(user) == {"id", "email", "full_name", "role", "is_active", "created_at"}
    dormant = users[4]
    assert (dormant["role"], dormant["is_active"]) == ("owner", False)
    assert users[0]["full_name"] == "Acme Owner"


# --- create ----------------------------------------------------------------


async def test_admin_creates_a_user_who_can_log_in(
    identity_client: AsyncClient, team: Team
) -> None:
    """The email is trimmed and lowercased; the password works at login."""
    body = {**NEW_USER, "email": "  New.Hire@Acme.TEST "}
    response = await _call(identity_client, team, "admin@acme.test", "POST", body=body)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["email"] == "new.hire@acme.test"
    assert (created["full_name"], created["role"], created["is_active"]) == (
        "New Hire",
        "operator",
        True,
    )
    login = await _login(identity_client, "new.hire@acme.test", "first-day-pass")
    assert login.status_code == 200
    assert login.json()["user"] == {
        "id": created["id"],
        "email": "new.hire@acme.test",
        "role": "operator",
        "tenant_id": team.members["owner@acme.test"][1],
    }


@pytest.mark.parametrize("email", ["viewer@acme.test", "VIEWER@acme.test"])
async def test_create_rejects_a_duplicate_email_in_the_tenant(
    identity_client: AsyncClient, team: Team, email: str
) -> None:
    """An email already used in the tenant (any case) is a conflict."""
    body = {**NEW_USER, "email": email}
    response = await _call(identity_client, team, "admin@acme.test", "POST", body=body)
    assert response.status_code == 409
    assert response.json() == {"detail": "email already registered"}


async def test_create_allows_an_email_of_another_tenant(
    identity_client: AsyncClient, team: Team
) -> None:
    """Emails are unique per tenant, so another tenant's email is free here."""
    body = {**NEW_USER, "email": "owner@globex.test"}
    response = await _call(identity_client, team, "admin@acme.test", "POST", body=body)
    assert response.status_code == 201


async def test_create_never_takes_the_tenant_from_the_client(
    identity_client: AsyncClient, team: Team
) -> None:
    """A tenant in the body is rejected; the tenant always comes from the token."""
    globex_id = team.members["owner@globex.test"][1]
    body = {**NEW_USER, "tenant_id": globex_id}
    response = await _call(identity_client, team, "admin@acme.test", "POST", body=body)
    assert response.status_code == 422
    async with team.db.sessionmaker() as session:
        count = await session.scalar(select(func.count()).where(User.tenant_id == globex_id))
    assert count == 1


@pytest.mark.parametrize(
    "override",
    [
        {"password": "short"},
        {"password": "é" * 40},  # 80 UTF-8 bytes: beyond bcrypt's 72-byte input
        {"email": "not-an-email"},
        {"email": "two words@acme.test"},
        {"role": "superadmin"},
        {"full_name": "   "},
    ],
)
async def test_create_validates_the_body(
    identity_client: AsyncClient, team: Team, override: dict
) -> None:
    """Invalid fields answer 422."""
    body = {**NEW_USER, **override}
    response = await _call(identity_client, team, "owner@acme.test", "POST", body=body)
    assert response.status_code == 422


async def test_only_an_owner_creates_an_owner(identity_client: AsyncClient, team: Team) -> None:
    """An admin cannot create an owner; an owner can."""
    body = {**NEW_USER, "role": "owner"}
    refused = await _call(identity_client, team, "admin@acme.test", "POST", body=body)
    assert refused.status_code == 403
    created = await _call(identity_client, team, "owner@acme.test", "POST", body=body)
    assert created.status_code == 201
    assert created.json()["role"] == "owner"


# --- role changes ----------------------------------------------------------


async def test_role_change_applies_to_the_users_next_request(
    identity_client: AsyncClient, team: Team
) -> None:
    """The database role wins over the role inside an already issued token."""
    viewer_headers = team.auth("viewer@acme.test")
    response = await _call(
        identity_client, team, "admin@acme.test", "PUT", "/{id}/role", {"role": "operator"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "operator"
    me = await identity_client.get("/api/v1/auth/me", headers=viewer_headers)
    assert me.json()["role"] == "operator"


@pytest.mark.parametrize("caller", ["owner@acme.test", "admin@acme.test"])
@pytest.mark.parametrize(
    ("method", "suffix", "body", "detail"),
    [
        ("PUT", "/{id}/role", {"role": "viewer"}, "cannot change your own role"),
        ("POST", "/{id}/deactivate", None, "cannot deactivate yourself"),
    ],
)
async def test_nobody_changes_their_own_role_or_deactivates_themselves(
    identity_client: AsyncClient,
    team: Team,
    caller: str,
    method: str,
    suffix: str,
    body,
    detail: str,
) -> None:
    """Self role changes and self deactivation are conflicts."""
    response = await _call(identity_client, team, caller, method, suffix, body, target=caller)
    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    me = await team.row(team.id(caller))
    assert me.role.value == team.members[caller][2]
    assert me.is_active


@pytest.mark.parametrize(
    ("method", "suffix", "body", "target"),
    [
        ("PUT", "/{id}/role", {"role": "owner"}, "viewer@acme.test"),
        ("PUT", "/{id}/role", {"role": "admin"}, "owner@acme.test"),
        ("POST", "/{id}/deactivate", None, "owner@acme.test"),
        ("POST", "/{id}/reactivate", None, "dormant@acme.test"),
    ],
)
async def test_an_admin_cannot_promote_to_or_modify_an_owner(
    identity_client: AsyncClient, team: Team, method: str, suffix: str, body, target: str
) -> None:
    """Owner-related changes are owner only; the target stays unchanged."""
    before = await team.row(team.id(target))
    response = await _call(
        identity_client, team, "admin@acme.test", method, suffix, body, target=target
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    after = await team.row(team.id(target))
    assert (after.role, after.is_active) == (before.role, before.is_active)


async def test_an_owner_manages_other_owners(identity_client: AsyncClient, team: Team) -> None:
    """An owner promotes, demotes, reactivates, and deactivates other owners."""
    owner = "owner@acme.test"
    promoted = await _call(
        identity_client, team, owner, "PUT", "/{id}/role", {"role": "owner"}, "admin@acme.test"
    )
    assert promoted.json()["role"] == "owner"
    demoted = await _call(
        identity_client, team, owner, "PUT", "/{id}/role", {"role": "admin"}, "admin@acme.test"
    )
    assert demoted.json()["role"] == "admin"
    revived = await _call(
        identity_client, team, owner, "POST", "/{id}/reactivate", target="dormant@acme.test"
    )
    assert revived.json()["is_active"] is True
    retired = await _call(
        identity_client, team, owner, "POST", "/{id}/deactivate", target="dormant@acme.test"
    )
    assert retired.status_code == 200
    assert retired.json()["is_active"] is False


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [("PUT", "/{id}/role", {"role": "admin"}), ("POST", "/{id}/deactivate", None)],
)
async def test_the_tenant_always_keeps_an_active_owner(
    identity_client: AsyncClient, team: Team, method: str, suffix: str, body
) -> None:
    """Removing the last active owner is refused (a superadmin passes the role checks)."""
    response = await _call(
        identity_client, team, "root@acme.test", method, suffix, body, target="owner@acme.test"
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "tenant must keep at least one active owner"}
    owner = await team.row(team.id("owner@acme.test"))
    assert (owner.role, owner.is_active) == (UserRole.OWNER, True)


# --- activation ------------------------------------------------------------


async def test_a_deactivated_user_cannot_log_in_or_use_old_tokens(
    identity_client: AsyncClient, team: Team
) -> None:
    """Deactivation blocks login and existing tokens; reactivation restores both."""
    login = await _login(identity_client, "viewer@acme.test", "viewer-pass")
    old_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await _call(identity_client, team, "admin@acme.test", "POST", "/{id}/deactivate")
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    refused = await _login(identity_client, "viewer@acme.test", "viewer-pass")
    assert refused.status_code == 401
    assert refused.json() == {"detail": "invalid credentials"}
    assert (await identity_client.get("/api/v1/auth/me", headers=old_headers)).status_code == 401

    response = await _call(identity_client, team, "admin@acme.test", "POST", "/{id}/reactivate")
    assert response.json()["is_active"] is True
    assert (await _login(identity_client, "viewer@acme.test", "viewer-pass")).status_code == 200
    assert (await identity_client.get("/api/v1/auth/me", headers=old_headers)).status_code == 200


# --- tenant isolation ------------------------------------------------------


@pytest.mark.parametrize(("method", "suffix", "body"), MUTATIONS)
async def test_another_tenants_user_is_not_found(
    identity_client: AsyncClient, team: Team, method: str, suffix: str, body
) -> None:
    """Other tenants' users answer exactly like unknown ids: 404, never 403."""
    foreign = await _call(
        identity_client, team, "owner@acme.test", method, suffix, body, "owner@globex.test"
    )
    unknown = await _call(identity_client, team, "owner@acme.test", method, suffix, body, 999_999)
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json() == {"detail": "user not found"}
    globex_owner = await team.row(team.id("owner@globex.test"))
    assert (globex_owner.role, globex_owner.is_active) == (UserRole.OWNER, True)


# --- audit -----------------------------------------------------------------


async def test_mutations_are_audited_without_secrets(
    identity_client: AsyncClient, team: Team, audit_log
) -> None:
    """Each change records one event; no password or hash reaches the trail."""
    admin = "admin@acme.test"
    created = await _call(identity_client, team, admin, "POST", body=NEW_USER)
    new_id = created.json()["id"]
    await _call(identity_client, team, admin, "PUT", "/{id}/role", {"role": "viewer"}, new_id)
    await _call(identity_client, team, admin, "POST", "/{id}/deactivate", target=new_id)
    await _call(identity_client, team, admin, "POST", "/{id}/deactivate", target=new_id)  # no-op
    await _call(identity_client, team, admin, "POST", "/{id}/reactivate", target=new_id)

    events = await audit_log(team.db, resource_type="user")
    assert [e.action for e in events] == [
        "user.created",
        "user.role_changed",
        "user.deactivated",
        "user.reactivated",
    ]
    assert {(e.actor_user_id, e.resource_id, e.tenant_id) for e in events} == {
        (team.id(admin), str(new_id), team.members[admin][1])
    }
    assert events[0].payload == {
        "email": "new.hire@acme.test",
        "full_name": "New Hire",
        "role": "operator",
    }
    assert events[1].payload == {"from": "operator", "to": "viewer"}
    stored_hash = (await team.row(new_id)).hashed_password
    trail = json.dumps([[e.payload, e.result] for e in events])
    assert NEW_USER["password"] not in trail
    assert stored_hash not in trail
    assert "password" not in trail


async def test_a_refused_change_is_audited(
    identity_client: AsyncClient, team: Team, audit_log
) -> None:
    """A privilege refusal persists ``user.denied`` with the operation."""
    response = await _call(
        identity_client,
        team,
        "admin@acme.test",
        "PUT",
        "/{id}/role",
        {"role": "viewer"},
        "owner@acme.test",
    )
    assert response.status_code == 403
    [event] = await audit_log(team.db, action="user.denied")
    assert event.actor_user_id == team.id("admin@acme.test")
    assert event.resource_id == str(team.id("owner@acme.test"))
    assert event.payload == {"operation": "change_role"}
    assert event.result == {"error": "forbidden"}
