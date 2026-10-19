"""Service writes must also work on a FILE-backed SQLite database.

The suite runs on ``sqlite+aiosqlite:///:memory:``, where SQLAlchemy uses a
single shared connection: two sessions on the same engine never contend, so
an independent audit transaction can never deadlock there. The production
engine opens a real file with a connection pool, and SQLite allows a single
writer: an audit session committing while the caller session holds a flushed
write raises ``sqlite3.OperationalError: database is locked``.

The calendar end-to-end flow starts with two production writes — activating
the module and creating the user's Google connection — so both are exercised
against a real file database here. No tenant/user parent rows are seeded:
SQLite foreign keys stay off unless a connection opts in, which is the
production default of this engine.
"""

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.db import Base, Database
from app.modules.audit.models import AuditEvent
from app.modules.calendar.registry import register_calendar
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.service import activate
from app.modules.connections.models import ConnectionStatus
from app.modules.connections.service import create_or_update
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole

CALLER = CurrentUser(
    id=2,
    tenant_id=1,
    email="admin@example.com",
    full_name="Admin",
    role=UserRole.OWNER,
    is_superadmin=False,
)


async def _file_database(tmp_path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/filedb.db")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return db


async def _audit_actions(db: Database, action: str) -> list[AuditEvent]:
    async with db.sessionmaker() as session:
        result = await session.execute(select(AuditEvent).where(AuditEvent.action == action))
        return list(result.scalars().all())


async def test_module_activation_commits_action_and_audit_on_a_file_db(tmp_path) -> None:
    """Activation writes the activation row and its audit event, no lock errors."""
    db = await _file_database(tmp_path)
    try:
        catalog = ModuleCatalog()
        register_calendar(catalog)

        async with db.sessionmaker() as session:
            row = await activate(session, catalog, CALLER, "calendar")
        assert row.active is True

        events = await _audit_actions(db, "module.activated")
        assert [event.resource_id for event in events] == ["calendar"]
    finally:
        await db.dispose()


async def test_connection_creation_commits_action_and_audit_on_a_file_db(tmp_path) -> None:
    """create_or_update writes the connection row and its audit event."""
    db = await _file_database(tmp_path)
    try:
        catalog = ModuleCatalog()
        register_calendar(catalog)
        credentials_key = Fernet.generate_key().decode()

        async with db.sessionmaker() as session:
            row = await create_or_update(
                session,
                catalog=catalog,
                credentials_key=credentials_key,
                caller=CALLER,
                module_key="calendar",
                provider="google",
                credentials='{"access_token": "tok", "scopes": []}',
            )
        assert row.status is ConnectionStatus.CONNECTED

        events = await _audit_actions(db, "connection.created")
        assert len(events) == 1
        assert events[0].resource_id == str(row.id)
    finally:
        await db.dispose()
