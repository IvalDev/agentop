"""Idempotent seed of the fictional demo company (development and tests only).

``python -m app.demo.seed [--create-schema] [--env-file .env] [--password PW] [--force]``

Creates the ``demo`` tenant, one user per role (see :mod:`app.demo.team`),
and ingests the synthetic business documents under ``app/demo/fixtures``
through the real documents service — with the demo owner as the acting user
— so chunks, offsets, embeddings, and audit events match a real upload.

Idempotency keys: the tenant by slug, users by (tenant, email), documents by
(tenant, filename). Existing rows are never mutated (a changed password or
role is not reset); a document whose earlier ingest failed is re-ingested
from its stored bytes. Edited fixture contents are not re-synced: reset the
development database to pick them up.

Guards: the seed refuses to run unless ``AA_ENVIRONMENT`` is a development
environment (``--force`` overrides with a loud warning). ``--create-schema``
runs ``Base.metadata.create_all`` (plus ``CREATE EXTENSION vector`` on
PostgreSQL); it is a development shortcut until the T12 baseline migration.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Base, Database
from app.demo.team import DEMO_TENANT_NAME, DEMO_TENANT_SLUG, DEMO_USERS, PASSWORD_ENV_VAR, DemoUser
from app.modules.approvals import models as _approval_models  # noqa: F401  (registers tables)
from app.modules.audit import models as _audit_models  # noqa: F401  (registers tables)
from app.modules.documents.models import Document, DocumentChunk, DocumentStatus
from app.modules.documents.service import create_document, ingest_document
from app.modules.documents.storage import DocumentStorage, LocalDocumentStorage
from app.modules.identity.models import Tenant, User, UserRole
from app.modules.identity.security import hash_password
from app.modules.retrieval.embeddings import EmbeddingProvider, build_embedding_provider

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEEDABLE_ENVIRONMENTS = frozenset({"dev", "development", "local", "test"})
_CONTENT_TYPES = {".md": "text/markdown", ".txt": "text/plain"}


class EnvironmentNotAllowedError(Exception):
    """Raised when seeding is attempted outside a development environment."""


@dataclass(frozen=True)
class SeededUser:
    """Outcome for one team member; ``password`` is ``None`` for pre-existing users."""

    email: str
    role: UserRole
    created: bool
    password: str | None


@dataclass(frozen=True)
class SeededDocument:
    """Outcome for one fixture: ``outcome`` is ``created``, ``existing``, or ``reingested``."""

    filename: str
    status: str
    chunks: int
    outcome: str
    error: str | None = None


@dataclass(frozen=True)
class SeedSummary:
    """What the seed found or created, ready to print."""

    tenant_id: int
    tenant_name: str
    tenant_created: bool
    users: list[SeededUser]
    documents: list[SeededDocument]

    @property
    def ok(self) -> bool:
        """``True`` when every fixture document ended up processed."""
        return all(item.status == DocumentStatus.PROCESSED.value for item in self.documents)


def check_environment(environment: str, *, force: bool) -> str | None:
    """Allow seeding only in development; return a warning when ``force`` overrides.

    Raises :class:`EnvironmentNotAllowedError` for any other environment
    (comparison is exact: ``PRODUCTION`` is not ``dev``).
    """
    if environment in SEEDABLE_ENVIRONMENTS:
        return None
    if not force:
        raise EnvironmentNotAllowedError(
            f"AA_ENVIRONMENT is {environment!r}; the demo seed only runs in "
            f"{sorted(SEEDABLE_ENVIRONMENTS)} (use --force to override)"
        )
    return (
        f"!!! WARNING: --force: seeding fictional demo data into environment {environment!r}. "
        "Demo users get development-only passwords. Never do this in a client installation. !!!"
    )


def load_env_file(path: Path) -> None:
    """Export ``KEY=value`` lines like ``uvicorn --env-file``: real env vars win."""
    if not path.is_file():
        raise FileNotFoundError(f"env file not found: {path}")
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)


def fixture_paths(directory: Path = FIXTURES_DIR) -> list[Path]:
    """The fixture documents to ingest, in a stable order."""
    return sorted(path for path in directory.iterdir() if path.suffix in _CONTENT_TYPES)


async def create_schema(db: Database) -> None:
    """Create missing tables (development only; T12 owns the real migrations)."""
    async with db.engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.create_all)


async def seed_demo(
    db: Database,
    *,
    storage: DocumentStorage,
    embeddings: EmbeddingProvider,
    settings: Settings,
    password: str | None = None,
    fixtures_dir: Path = FIXTURES_DIR,
) -> SeedSummary:
    """Ensure the demo tenant, team, and documents exist; return what happened.

    ``password`` overrides every new user's default development password.
    Each document gets its own session and commits: an ingest failure
    rolls back that session, and must not take other documents with it.
    """
    async with db.sessionmaker() as session:
        tenant, tenant_created = await _ensure_tenant(session)
        team = [await _ensure_user(session, tenant.id, spec, password) for spec in DEMO_USERS]
        await session.commit()
    # The actor is the team's owner seat, even if someone changed that user's role since.
    owner_id = next(
        user.id
        for spec, (user, _) in zip(DEMO_USERS, team, strict=True)
        if spec.role == UserRole.OWNER
    )

    documents = []
    for path in fixture_paths(fixtures_dir):
        async with db.sessionmaker() as session:
            documents.append(
                await _ensure_document(
                    session, tenant.id, owner_id, path, storage, embeddings, settings
                )
            )
    return SeedSummary(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_created=tenant_created,
        users=[seeded for _, seeded in team],
        documents=documents,
    )


async def _ensure_tenant(session: AsyncSession) -> tuple[Tenant, bool]:
    result = await session.execute(select(Tenant).where(Tenant.slug == DEMO_TENANT_SLUG))
    tenant = result.scalar_one_or_none()
    if tenant is not None:
        return tenant, False
    tenant = Tenant(name=DEMO_TENANT_NAME, slug=DEMO_TENANT_SLUG, is_active=True)
    session.add(tenant)
    await session.flush()
    return tenant, True


async def _ensure_user(
    session: AsyncSession, tenant_id: int, spec: DemoUser, password: str | None
) -> tuple[User, SeededUser]:
    result = await session.execute(
        select(User).where(User.tenant_id == tenant_id, User.email == spec.email)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user, SeededUser(user.email, user.role, created=False, password=None)
    plain = password or spec.default_password
    user = User(
        tenant_id=tenant_id,
        email=spec.email,
        full_name=spec.full_name,
        hashed_password=hash_password(plain),
        role=spec.role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user, SeededUser(user.email, user.role, created=True, password=plain)


async def _ensure_document(
    session: AsyncSession,
    tenant_id: int,
    owner_id: int,
    path: Path,
    storage: DocumentStorage,
    embeddings: EmbeddingProvider,
    settings: Settings,
) -> SeededDocument:
    result = await session.execute(
        select(Document).where(Document.tenant_id == tenant_id, Document.filename == path.name)
    )
    document = result.scalars().first()
    if document is not None and document.status == DocumentStatus.PROCESSED:
        chunks = await _chunk_count(session, tenant_id, document)
        return SeededDocument(path.name, document.status.value, chunks, "existing")
    if document is None:
        document = await create_document(
            session,
            tenant_id,
            path.name,
            _CONTENT_TYPES[path.suffix],
            path.read_bytes(),
            storage,
            settings=settings,
            actor_user_id=owner_id,
        )
        # The row must be durable before ingest: an embedding failure rolls the session back.
        await session.commit()
        outcome = "created"
    else:
        outcome = "reingested"
    try:
        await ingest_document(
            session,
            tenant_id,
            document.id,
            storage,
            embeddings=embeddings,
            actor_user_id=owner_id,
        )
        await session.commit()
    except DBAPIError:
        raise
    except Exception as exc:  # ingest_document already committed the failed status + audit
        error = str(exc) or type(exc).__name__
        return SeededDocument(path.name, DocumentStatus.FAILED.value, 0, outcome, error)
    chunks = await _chunk_count(session, tenant_id, document)
    return SeededDocument(path.name, DocumentStatus.PROCESSED.value, chunks, outcome)


async def _chunk_count(session: AsyncSession, tenant_id: int, document: Document) -> int:
    query = (
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.tenant_id == tenant_id, DocumentChunk.document_id == document.id)
    )
    return (await session.execute(query)).scalar_one()


def format_summary(summary: SeedSummary) -> str:
    """Human-readable report, including the credentials of newly created users."""
    state = "created" if summary.tenant_created else "existing"
    lines = [
        f"Demo company: {summary.tenant_name} "
        f"(tenant slug '{DEMO_TENANT_SLUG}', id {summary.tenant_id}) [{state}]",
        "",
        f"Team (development-only credentials; log in with tenant slug '{DEMO_TENANT_SLUG}'):",
    ]
    for user in summary.users:
        secret = user.password if user.password is not None else "(unchanged)"
        state = "created" if user.created else "existing"
        lines.append(f"  {user.role.value:<9} {user.email:<22} password: {secret:<22} [{state}]")
    processed = sum(item.status == DocumentStatus.PROCESSED.value for item in summary.documents)
    failed = len(summary.documents) - processed
    lines += ["", f"Documents: {processed} processed, {failed} failed"]
    for item in summary.documents:
        detail = f"{item.chunks} chunks" if item.error is None else f"error: {item.error}"
        lines.append(f"  [{item.outcome:<10}] {item.filename:<45} {item.status}, {detail}")
    if failed:
        lines += [
            "",
            "Some documents failed to ingest. Fix the cause (for example start Ollama, or set "
            "AA_EMBEDDING_PROVIDER=fake) and run the seed again: failed documents are retried.",
        ]
    return "\n".join(lines)


async def _run(settings: Settings, *, create_schema_first: bool, password: str | None):
    db = Database(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    try:
        if create_schema_first:
            await create_schema(db)
        return await seed_demo(
            db,
            storage=LocalDocumentStorage(settings.storage_base_dir),
            embeddings=build_embedding_provider(settings),
            settings=settings,
            password=password,
        )
    finally:
        await db.dispose()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.demo.seed",
        description="Seed the fictional demo company (development only).",
    )
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="create missing tables first (dev only; no migrations exist yet)",
    )
    parser.add_argument(
        "--env-file", type=Path, help="load AA_* settings from this file (real env vars win)"
    )
    parser.add_argument(
        "--password",
        help=f"password for every new demo user (default: ${PASSWORD_ENV_VAR}, "
        "else demo-<role>-pass)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="seed even when AA_ENVIRONMENT is not a development environment",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = _parse_args(argv)
    try:
        if args.env_file is not None:
            load_env_file(args.env_file)
        settings = Settings()
    except (FileNotFoundError, ValidationError) as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    try:
        warning = check_environment(settings.environment, force=args.force)
    except EnvironmentNotAllowedError as exc:
        print(f"refusing to seed: {exc}", file=sys.stderr)
        return 2
    if warning is not None:
        print(warning, file=sys.stderr)
    if not settings.database_url:
        print("AA_DATABASE_URL must be set (see deployment/dev/*.env.example)", file=sys.stderr)
        return 2

    password = args.password or os.environ.get(PASSWORD_ENV_VAR) or None
    try:
        summary = asyncio.run(
            _run(settings, create_schema_first=args.create_schema, password=password)
        )
    except DBAPIError as exc:
        print(
            f"database error: {exc.orig}\n"
            "Missing tables? For a development database run again with --create-schema.",
            file=sys.stderr,
        )
        return 1
    print(format_summary(summary))
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
