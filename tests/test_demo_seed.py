"""Demo company seed: tenant, team, synthetic documents, idempotency, and guards.

The seed runs through the real documents service (local storage, fake
embeddings) so the demo data has the same chunks, offsets, embeddings, and
audit events as an upload through the API.
"""

import os
import re
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.db import Database
from app.demo.chat import iter_sse
from app.demo.seed import (
    FIXTURES_DIR,
    EnvironmentNotAllowedError,
    check_environment,
    fixture_paths,
    load_env_file,
    main,
    seed_demo,
)
from app.demo.team import DEMO_TENANT_SLUG, DEMO_USERS
from app.main import create_app
from app.modules.agent.llm import AssistantMessage, FakeLLMProvider, ToolCall
from app.modules.audit.models import AuditEvent
from app.modules.documents.extract import extract_text
from app.modules.documents.models import Document, DocumentChunk, DocumentStatus
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.models import Tenant, User, UserRole
from app.modules.retrieval.embeddings import EmbeddingError, FakeEmbeddingProvider

DIM = 64
JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"


@pytest.fixture
def storage(tmp_path: Path) -> LocalDocumentStorage:
    return LocalDocumentStorage(tmp_path / "storage")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        jwt_secret=JWT_SECRET,
        embedding_provider="fake",
        embedding_dim=DIM,
        llm_provider="fake",
        storage_base_dir=str(tmp_path / "storage"),
    )


async def _seed(db: Database, storage, settings, **kwargs):
    kwargs.setdefault("embeddings", FakeEmbeddingProvider(dimension=DIM))
    return await seed_demo(db, storage=storage, settings=settings, **kwargs)


async def _counts(db: Database) -> dict[str, int]:
    async with db.sessionmaker() as session:

        async def count(model) -> int:
            return (await session.execute(select(func.count()).select_from(model))).scalar_one()

        return {
            "tenants": await count(Tenant),
            "users": await count(User),
            "documents": await count(Document),
            "chunks": await count(DocumentChunk),
            "audit_events": await count(AuditEvent),
        }


def test_fixture_set_covers_the_demo_business_documents() -> None:
    names = [path.name for path in fixture_paths()]
    assert len([name for name in names if name.startswith("factura_")]) >= 3
    assert len([name for name in names if name.startswith("contrato_")]) >= 2
    assert len([name for name in names if name.startswith("correo_")]) >= 3
    assert len([name for name in names if name.startswith("procedimiento_")]) >= 1
    assert len([name for name in names if name.startswith("empresa_")]) >= 5
    assert len([name for name in names if name.startswith("empleado_")]) >= 6
    assert len([name for name in names if name.startswith("rrhh_")]) >= 2
    assert all(path.suffix in {".md", ".txt"} for path in fixture_paths())


async def test_seed_creates_tenant_team_and_ingested_documents(
    identity_db: Database, storage: LocalDocumentStorage, settings: Settings
) -> None:
    summary = await _seed(identity_db, storage, settings)

    async with identity_db.sessionmaker() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == DEMO_TENANT_SLUG))
        ).scalar_one()
        users = (
            (await session.execute(select(User).where(User.tenant_id == tenant.id))).scalars().all()
        )
        documents = (
            (await session.execute(select(Document).where(Document.tenant_id == tenant.id)))
            .scalars()
            .all()
        )
        chunks = (await session.execute(select(DocumentChunk))).scalars().all()
        ingested = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.ingested")
                )
            )
            .scalars()
            .all()
        )

    assert summary.tenant_created is True
    assert tenant.is_active and "demo" in tenant.name.lower()
    assert {user.role for user in users} == set(UserRole)
    assert {user.email for user in users} == {spec.email for spec in DEMO_USERS}
    assert all(user.email.endswith("@demo.test") for user in users)

    assert len(documents) == len(fixture_paths())
    assert all(document.status == DocumentStatus.PROCESSED for document in documents)
    assert summary.ok and all(item.outcome == "created" for item in summary.documents)
    by_document = {document.id: document for document in documents}
    assert {chunk.document_id for chunk in chunks} == set(by_document)
    for chunk in chunks:
        assert chunk.tenant_id == tenant.id
        assert chunk.embedding is not None and len(chunk.embedding) == DIM
        document = by_document[chunk.document_id]
        text = extract_text(await storage.get(document.storage_key), document.content_type)
        assert text[chunk.start_offset : chunk.end_offset] == chunk.content

    owner = next(user for user in users if user.role == UserRole.OWNER)
    assert len(ingested) == len(documents)
    assert {event.actor_user_id for event in ingested} == {owner.id}
    assert {event.tenant_id for event in ingested} == {tenant.id}


async def test_seeded_team_logs_in_with_the_printed_credentials(
    identity_db: Database, storage: LocalDocumentStorage, settings: Settings
) -> None:
    summary = await _seed(identity_db, storage, settings)
    application = create_app()
    application.state.db = identity_db
    application.state.settings = settings

    credentials = {user.email: user for user in summary.users}
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        for spec in DEMO_USERS:
            seeded = credentials[spec.email]
            assert seeded.created and seeded.password == spec.default_password
            response = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": spec.email,
                    "password": seeded.password,
                    "tenant_slug": DEMO_TENANT_SLUG,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["user"]["role"] == spec.role.value


async def test_seed_password_override_applies_to_the_whole_team(
    identity_db: Database, storage: LocalDocumentStorage, settings: Settings
) -> None:
    summary = await _seed(identity_db, storage, settings, password="otra-clave-solo-dev")
    assert {user.password for user in summary.users} == {"otra-clave-solo-dev"}


async def test_seed_twice_is_idempotent(
    identity_db: Database, storage: LocalDocumentStorage, settings: Settings
) -> None:
    await _seed(identity_db, storage, settings)
    first = await _counts(identity_db)

    summary = await _seed(identity_db, storage, settings)

    assert await _counts(identity_db) == first
    assert summary.tenant_created is False
    assert all(not user.created and user.password is None for user in summary.users)
    assert all(item.outcome == "existing" for item in summary.documents)
    assert summary.ok


class BrokenEmbeddings:
    """Embedding backend that is down (e.g. Ollama not running)."""

    dimension = DIM

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("ollama embeddings request failed: connection refused")


async def test_seed_rerun_retries_documents_whose_ingest_failed(
    identity_db: Database, storage: LocalDocumentStorage, settings: Settings
) -> None:
    failed = await _seed(identity_db, storage, settings, embeddings=BrokenEmbeddings())
    assert not failed.ok
    assert all(item.status == "failed" and item.error for item in failed.documents)

    retried = await _seed(identity_db, storage, settings)

    assert retried.ok
    assert all(item.outcome == "reingested" for item in retried.documents)
    counts = await _counts(identity_db)
    assert counts["documents"] == len(fixture_paths())


@pytest.mark.parametrize("environment", ["dev", "development", "local", "test"])
def test_environment_guard_allows_development_environments(environment: str) -> None:
    assert check_environment(environment, force=False) is None


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "", "PRODUCTION"])
def test_environment_guard_refuses_other_environments(environment: str) -> None:
    with pytest.raises(EnvironmentNotAllowedError):
        check_environment(environment, force=False)


def test_environment_guard_force_returns_a_loud_warning() -> None:
    warning = check_environment("production", force=True)
    assert warning is not None and "WARNING" in warning and "production" in warning


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str = "dev") -> Path:
    db_file = tmp_path / "dev.db"
    monkeypatch.setenv("AA_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("AA_STORAGE_BASE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("AA_ENVIRONMENT", environment)
    monkeypatch.setenv("AA_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("AA_CREDENTIALS_KEY", "cqsp933navSzGBmyUEnO6EKloifkQMdohIhgjgTQ3iE=")
    monkeypatch.delenv("AA_DEMO_PASSWORD", raising=False)
    return db_file


def test_cli_refuses_production_without_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_file = _cli_env(monkeypatch, tmp_path, environment="production")

    assert main(["--create-schema"]) == 2

    assert "refusing" in capsys.readouterr().err
    assert not db_file.exists()


def test_cli_force_seeds_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(monkeypatch, tmp_path, environment="production")

    assert main(["--create-schema", "--force"]) == 0

    assert "WARNING" in capsys.readouterr().err


def test_cli_seeds_a_sqlite_file_twice_and_prints_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_file = _cli_env(monkeypatch, tmp_path)

    assert main(["--create-schema"]) == 0
    first = capsys.readouterr().out
    assert main(["--create-schema"]) == 0
    second = capsys.readouterr().out

    assert db_file.exists()
    for spec in DEMO_USERS:
        assert spec.email in first and spec.default_password in first
        assert spec.email in second and spec.default_password not in second
    assert "created" in first and "existing" in second


def test_cli_no_documents_seeds_only_the_tenant_and_team(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_file = _cli_env(monkeypatch, tmp_path)

    assert main(["--create-schema", "--no-documents"]) == 0

    out = capsys.readouterr().out
    for spec in DEMO_USERS:
        assert spec.email in out
    assert "Documents: 0 processed, 0 failed" in out
    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)


def test_cli_without_schema_on_an_empty_database_hints_the_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(monkeypatch, tmp_path)

    assert main([]) == 1

    assert "--create-schema" in capsys.readouterr().err


def test_load_env_file_exports_values_without_overriding_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text("# comment\nAA_DEMO_SENTINEL_A=from-file\nAA_DEMO_SENTINEL_B=from-file\n")
    monkeypatch.setenv("AA_DEMO_SENTINEL_A", "from-shell")
    monkeypatch.setenv("AA_DEMO_SENTINEL_B", "placeholder")
    monkeypatch.delenv("AA_DEMO_SENTINEL_B")

    load_env_file(env_file)

    assert os.environ["AA_DEMO_SENTINEL_A"] == "from-shell"
    assert os.environ["AA_DEMO_SENTINEL_B"] == "from-file"


async def test_demo_answers_with_a_citation_over_a_sqlite_file(
    tmp_path: Path, storage: LocalDocumentStorage, settings: Settings
) -> None:
    """The zero-infrastructure path: SQLite file + seed + chat stream with a tool call."""
    from app.demo.seed import create_schema

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'dev.db'}")
    try:
        await create_schema(db)
        await _seed(db, storage, settings)
        application = create_app()
        application.state.db = db
        application.state.settings = settings
        application.state.embeddings = FakeEmbeddingProvider(dimension=DIM)
        application.state.llm = FakeLLMProvider(
            [
                AssistantMessage(
                    content="",
                    tool_calls=(
                        ToolCall("search_documents", {"query": "TIS-2026-0150", "mode": "keyword"}),
                    ),
                ),
                AssistantMessage(content="La factura TIS-2026-0150 suma 1.452,00 € [1]."),
            ]
        )
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": "viewer@demo.test",
                    "password": "demo-viewer-pass",
                    "tenant_slug": DEMO_TENANT_SLUG,
                },
            )
            token = login.json()["access_token"]
            response = await client.post(
                "/api/v1/chat/stream",
                json={"message": "¿Cuánto suma la factura de transporte?"},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        await db.dispose()

    events = list(iter_sse(response.text.splitlines()))
    types = [event.type for event in events]
    assert "error" not in types, events
    assert types[0] == "run_started" and types[-1] == "done"
    done = events[-1].data
    assert len(done["citations"]) == 1
    shown = {source["document_filename"] for source in done["sources"]}
    assert "factura_TIS-2026-0150_transporte.txt" in shown


FICTIONAL_FISCAL_ID = re.compile(r"^[A-Z]000000\d\d$")
FISCAL_ID_SHAPE = re.compile(r"\b[A-HJNPQRSUVW]\d{7}[0-9A-J]\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IBAN = re.compile(r"\bES\d{2}(?:\s?\d{4}){5}\b")
SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|xai|pk|rk)-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]+-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(?:password|passwd|contraseña|api[_ ]?key|token)\s*[:=]"),
    re.compile(r"\b[A-Za-z0-9+/_-]{32,}\b"),
]


@pytest.mark.parametrize("path", fixture_paths(), ids=lambda path: path.name)
def test_fixture_contains_only_obviously_fictional_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), f"secret-like text {pattern.pattern!r} in {path.name}"
    for email in EMAIL.findall(text):
        assert email.endswith(".test"), f"non-.test email {email} in {path.name}"
    for fiscal_id in FISCAL_ID_SHAPE.findall(text):
        assert FICTIONAL_FISCAL_ID.match(fiscal_id), f"real-looking CIF/NIF {fiscal_id}"
    for iban in IBAN.findall(text):
        assert set(re.sub(r"\D", "", iban)) == {"0"}, f"real-looking IBAN {iban}"
    assert "ficticio" in text.lower()


STAFF_DIRECTORY = "empresa_organigrama_directorio_personal.md"
EMPLOYEE_RECORDS = [path for path in fixture_paths() if path.name.startswith("empleado_")]
FULL_NAME_FIELD = re.compile(r"^- \*\*Nombre completo:\*\* (.+)$", re.MULTILINE)
DNI_OR_NIE = re.compile(r"\b[XYZ]?\d{7,8}-?[A-Z]\b")
IBAN_LIKE = re.compile(r"\bES\d{2}")
PAY_TERMS = re.compile(r"(?i)\b(?:salario|sueldo|n[oó]minas?|retribuci[oó]n|bruto anual)\b")


def _employee_names() -> dict[str, str]:
    """The full name each employee record declares, keyed by file name."""
    names = {}
    for path in EMPLOYEE_RECORDS:
        match = FULL_NAME_FIELD.search(path.read_text(encoding="utf-8"))
        assert match, f"{path.name} declares no '- **Nombre completo:** ...' field"
        names[path.name] = match.group(1).strip()
    return names


def _sensitive_findings(text: str) -> list[str]:
    """ID numbers, bank accounts, and pay amounts (a pay term and a digit on one line)."""
    findings = DNI_OR_NIE.findall(text) + IBAN_LIKE.findall(text)
    findings += [
        line for line in text.splitlines() if PAY_TERMS.search(line) and re.search(r"\d", line)
    ]
    return findings


def test_every_demo_user_has_an_employee_record() -> None:
    recorded = set(_employee_names().values())

    missing = sorted(spec.full_name for spec in DEMO_USERS if spec.full_name not in recorded)

    assert not missing, f"no empleado_*.md record for {missing}"


def test_staff_directory_lists_every_employee_and_the_headcount() -> None:
    directory = (FIXTURES_DIR / STAFF_DIRECTORY).read_text(encoding="utf-8")
    names = _employee_names()
    people = set(names.values()) | {spec.full_name for spec in DEMO_USERS}

    missing = sorted(name for name in people if name not in directory)

    assert not missing, f"{STAFF_DIRECTORY} does not list {missing}"
    assert f"{len(names)} personas" in directory


@pytest.mark.parametrize(
    "text",
    [
        "DNI: 12345678Z",
        "NIE X1234567L",
        "Cuenta: ES76 1234 5678 9012 3456 7890",
        "Salario bruto anual: 24.000 €",
        "Nómina de marzo de 1.800,00 €",
    ],
)
def test_sensitive_data_guard_flags_personal_identifiers_and_pay(text: str) -> None:
    assert _sensitive_findings(text)


def test_sensitive_data_guard_accepts_work_data() -> None:
    text = "Aprueba compras de hasta 1.000 € (PRC-FIN-02).\nPrepara las nóminas con la asesoría."

    assert _sensitive_findings(text) == []


@pytest.mark.parametrize("path", EMPLOYEE_RECORDS, ids=lambda path: path.name)
def test_employee_record_omits_sensitive_personal_data(path: Path) -> None:
    """Documents have no per-role permissions: any role, viewer included, reads them."""
    text = path.read_text(encoding="utf-8")

    assert _sensitive_findings(text) == [], path.name


def test_demo_team_is_obviously_fictional() -> None:
    assert [spec.role for spec in DEMO_USERS] == list(UserRole)
    for spec in DEMO_USERS:
        assert spec.email == f"{spec.role.value}@demo.test"
        assert "demo" in spec.full_name.lower()
