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
make demo      # seed the fictional demo company (dev only; creates the schema)
make dev       # like `make run`, reading settings from .env when present
make chat      # console chat client; `make chat user=viewer` for another role
make app       # build the React web app (web/) and serve it with the API on :8000
make web-dev   # Vite dev server with hot reload on :5173 (run `make dev` alongside)
```

The web app (React + Vite + TypeScript, Spanish UI) needs Node 22.22+; see
[docs/dev-setup.md](docs/dev-setup.md#web-app).

**Light setup without a VPS** (SQLite, Grok as the chat model, no local inference):
see [docs/dev-setup.md](docs/dev-setup.md). Profiles live in `deployment/dev/`.

### Chat model settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `AA_LLM_PROVIDER` | `ollama` | `ollama` (local), `fake` (offline tests), or `openai_compatible` (external API) |
| `AA_LLM_MODEL` | `qwen2.5:7b-instruct` | Model name for the selected provider |
| `AA_LLM_BASE_URL` | empty | `openai_compatible` only, e.g. `https://api.x.ai/v1` |
| `AA_LLM_API_KEY` | empty | `openai_compatible` only; never logged or audited |
| `AA_ALLOW_EXTERNAL_LLM` | `false` | Must be `true` for `openai_compatible`, which sends prompts and document passages off the machine — synthetic data only, never in client installations |
| `AA_LLM_TIMEOUT_SECONDS` | `120` | Per-request model timeout |
| `AA_AGENT_MAX_STEPS` | `6` | Model calls per chat run (1–50) |

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
