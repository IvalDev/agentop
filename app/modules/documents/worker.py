"""Ingest worker entrypoint.

Queue binding (Redis+RQ per decision A1) lands in T12; this coroutine is
the thin unit this queue will call: one document, one isolated session,
local storage. Extraction is synchronous by design (see
:mod:`app.modules.documents.extract`) so it can later move to an RQ
worker process without signature changes.
"""

from uuid import UUID

from app.core.config import Settings
from app.core.db import Database, session_scope
from app.modules.documents.service import ingest_document
from app.modules.documents.storage import LocalDocumentStorage


async def run_ingest(document_id: UUID, tenant_id: int) -> None:
    """Ingest one document with an isolated DB session and local storage.

    ``tenant_id`` is an int because tenants have integer primary keys
    (see identity models); T12 may inject a shared engine/settings pair
    instead of building them per run.
    """
    settings = Settings()
    db = Database(settings.database_url)
    try:
        async with session_scope(db) as session:
            storage = LocalDocumentStorage(settings.storage_base_dir)
            await ingest_document(session, tenant_id, document_id, storage)
    finally:
        await db.dispose()
