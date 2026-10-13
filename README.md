# admin-agent

Private, local-first AI administrative agent for Spanish SMBs: a modular FastAPI monolith
that provides tenant-isolated identity, document ingestion with RAG and verifiable
citations, a typed tool engine with human approval for sensitive actions, and append-only
auditing — so any administrative workflow (invoices, email, tasks, reports) can be added as
a module without touching the core. Inference runs locally via Ollama for privacy.

Full feature plan and task checklist: [odd/tasks/admin-agent-core.md](odd/tasks/admin-agent-core.md).

## Stack

| Layer | Technology |
| --- | --- |
| Language | Python 3.12+ |
| API | FastAPI + Pydantic v2 |
| Database | PostgreSQL 16 + pgvector |
| Queue | Redis 7 + RQ |
| Object storage | MinIO (S3 compatible) |
| Inference | Ollama |
| Tooling | pytest, ruff, httpx |

## Local development

```bash
make install   # pip install -e ".[dev]"
make lint      # ruff check . && ruff format --check .
make format    # ruff format . && ruff check --fix .
make test      # pytest
make run       # uvicorn app.main:app --reload
```

## Database migrations

Schema changes are managed with Alembic; migrations read `AA_DATABASE_URL` from the
environment (see `.env.example`):

```bash
make migrate                        # alembic upgrade head
make migrate-revision m="add users" # alembic revision --autogenerate -m "add users"
```

## Docker Compose

```bash
cp .env.example .env
docker compose -f deployment/docker-compose.yml up --build
```

This starts the API on port 8000 plus Postgres, Redis, MinIO (console on 9001), and
Ollama. Verify with `curl localhost:8000/healthz`.
