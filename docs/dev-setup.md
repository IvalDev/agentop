# Light development setup (no VPS, weak machine)

Run the whole agent on a modest laptop against a **fictional demo company**: one SQLite
file instead of Postgres, the chat model hosted by xAI (Grok) instead of local inference,
and no embedding model at all (or a single small one). You get a seeded team with one user
per role, about a dozen synthetic Spanish business documents, and a console chat client.

> **Privacy warning — read before using an external model.**
> With the Grok profile every chat message, the conversation history, and every document
> passage the agent reads is sent to `api.x.ai`. Use it **only with the synthetic demo
> data** created by `make demo`. Never load real customer documents while an external
> provider is configured, and never use an external provider in a client installation:
> local inference with Ollama is the product default. Embeddings always stay local.

## Quick path: SQLite + Grok + no embedding model

Prerequisites: Python 3.12+, an xAI API key. Run everything from the repository root.

1. Install:

   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   make install
   ```

2. Configure — `.env` is git-ignored, so the key never reaches the repository:

   ```bash
   cp deployment/dev/grok.env.example .env
   ```

   Edit `.env`: set `AA_LLM_API_KEY`, generate `AA_JWT_SECRET`
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), and check that
   `AA_LLM_MODEL` names a current Grok model with function calling in the xAI console.

3. Seed the demo company (creates `dev.db` and the tables, then prints the credentials):

   ```bash
   make demo
   ```

4. Start the API on port 8000 and check it:

   ```bash
   make dev
   curl localhost:8000/healthz    # {"status":"ok","database":"ok"}
   ```

   The startup log warns that the external provider is enabled — that is expected.

5. Chat, in a second terminal (with the virtual environment activated):

   ```bash
   make chat              # as the owner
   make chat user=viewer  # as any other role
   ```

Questions to try (the documents are in Spanish):

- ¿Qué facturas de proveedores están pendientes de pago y cuándo vencen?
- ¿Quién tiene que aprobar la factura TIS-2026-0150 según el procedimiento interno?
- ¿Cuánto pagamos al mes por la oficina, IVA incluido?
- ¿Qué reclama Comercial Ejemplo Levante sobre la factura V-2026-0031?

## What the commands do

| Command | Runs | Notes |
| --- | --- | --- |
| `make demo` | `python -m app.demo.seed --create-schema [--env-file .env]` | Idempotent: re-running never duplicates the tenant, users, or documents |
| `make dev` | `uvicorn app.main:app --reload [--env-file .env]` | Same as `make run`, plus the env file |
| `make chat user=<role>` | `python -m app.demo.chat --user <role>` | Logs in as `<role>@demo.test` against `http://localhost:8000` |

`make demo` and `make dev` read `.env` only when it exists (`make demo ENV_FILE=other.env`
picks another file). Variables already exported in the shell win over the file.

### The seed (`python -m app.demo.seed`)

| Flag | Effect |
| --- | --- |
| `--create-schema` | Creates missing tables with `Base.metadata.create_all` (and `CREATE EXTENSION vector` on Postgres). **Development only**: there are no Alembic migrations yet. |
| `--env-file PATH` | Loads `AA_*` settings from a file, like `uvicorn --env-file`. |
| `--password PW` | Password for every newly created demo user (also `AA_DEMO_PASSWORD`). Default: `demo-<role>-pass`. |
| `--force` | Seeds even when `AA_ENVIRONMENT` is not `dev`, `development`, `local`, or `test`, with a loud warning. Without it the seed refuses. |

It creates the tenant **Suministros Ficticios Demo, S.L.** (slug `demo`), its team, and the
documents in `app/demo/fixtures/` — five invoices, two contracts, three emails, and an
internal payment procedure — ingested through the real documents service with the owner
as the acting user, so chunks, citation offsets, embeddings, and audit events are real.

Re-running keeps existing rows untouched (a changed password is not reset; the summary
shows `(unchanged)`) and retries documents whose ingest failed, for example because
Ollama was not running. Edited fixture files are not re-synced: reset the database.

### The console client (`python -m app.demo.chat`)

Tokens stream inline; tool calls, tool results, and approval-required notices (with the
approval id) appear on their own lines; cited passages are listed after each answer.
`/reset` clears the conversation history, `/quit` (or Ctrl-D) exits, and `-m "..."` sends
one message and exits. Other flags: `--base-url`, `--email`, `--password`, `--tenant`,
`--timeout`. If you seeded with a custom password, pass `--password` or export
`AA_DEMO_PASSWORD` in the client's shell — the client does not read `.env`.

## The demo team

Every role logs in with tenant slug `demo`.

| Role | Email | Default password | Can do |
| --- | --- | --- | --- |
| owner | `owner@demo.test` | `demo-owner-pass` | Everything below, plus read the audit log (`GET /api/v1/audit`) |
| admin | `admin@demo.test` | `demo-admin-pass` | Same as owner in this milestone |
| operator | `operator@demo.test` | `demo-operator-pass` | Chat, search, upload and ingest documents; review approvals for operator-level tools |
| viewer | `viewer@demo.test` | `demo-viewer-pass` | Chat, search, list documents (read-only) |

The agent only offers the tools a role may run, and an approval request can only be seen
and decided by a role that satisfies the tool's required role. Today the only built-in
tool is `search_documents` (viewer and up), so no chat run produces an approval request
yet; the client already renders them for the action tools that come later.

## Embeddings on a weak machine

| Option | Settings | Ranking quality |
| --- | --- | --- |
| No model (default in `grok.env.example`) | `AA_EMBEDDING_PROVIDER=fake` | Semantic and hybrid search still run, but the "fake" vectors are hashed words: results rank by shared words, not meaning |
| One small local model | `AA_EMBEDDING_PROVIDER=ollama`, `AA_EMBEDDING_MODEL=nomic-embed-text`, `AA_EMBEDDING_DIM=768`; `ollama pull nomic-embed-text` | Real semantic similarity; Ollama runs no chat model |

Changing the embedding provider or model after seeding mixes incompatible vectors: delete
`dev.db` and `var/storage` and run `make demo` again.

## SQLite or Postgres

The app runs on both; the test suite runs on SQLite.

| Concern | SQLite file (`sqlite+aiosqlite:///./dev.db`) | Postgres (pgvector) |
| --- | --- | --- |
| Setup | Nothing to install | Docker container |
| Semantic search | Cosine similarity computed in Python over the tenant's chunks — fine for demo-sized data | Computed by pgvector in the database |
| Vector size | Any (stored as JSON) | Fixed at 768 dimensions: keep `AA_EMBEDDING_DIM=768` |
| Concurrency | One writer at a time — fine for one developer | Production-like |

### Postgres path

```bash
docker compose -f deployment/docker-compose.yml -f deployment/dev/compose.postgres.yml up -d postgres
```

This starts only the Postgres service of the main compose file and publishes it on
`127.0.0.1:5432`. In `.env` set:

```bash
AA_DATABASE_URL=postgresql+asyncpg://admin:admin@localhost:5432/admin_agent
```

Then `make demo`, `make dev`, and `make chat` exactly as above.

## Local-only profile

`deployment/dev/ollama.env.example` is the product default: chat and embeddings run in a
local Ollama (`qwen2.5:7b-instruct` plus `nomic-embed-text`), and nothing leaves the
machine. It needs several GB of free RAM for the chat model.

## Reset

```bash
rm -f dev.db && rm -rf var/storage && make demo
```

For Postgres, drop the database volume instead of `dev.db` (other volumes, such as
downloaded Ollama models, are kept):

```bash
docker compose -f deployment/docker-compose.yml rm -sf postgres
docker volume rm deployment_postgres_data
```

Keep `dev.db`, `var/`, and `.env` out of commits.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `refusing to seed: AA_ENVIRONMENT is ...` | The seed runs only in development environments; fix `AA_ENVIRONMENT` (or `--force` if you are sure) |
| `database error ... Missing tables?` | The database has no schema: use `make demo` (it passes `--create-schema`) |
| The API refuses to start with `llm_provider 'openai_compatible' ...` | Set `AA_ALLOW_EXTERNAL_LLM=true`, `AA_LLM_API_KEY`, and `AA_LLM_BASE_URL` |
| Chat prints `[error] language model unavailable` | Wrong key or model name, or the provider is unreachable; the API log says which |
| Documents `failed` with `ollama embeddings request failed` | Start Ollama and pull the embedding model, or use `AA_EMBEDDING_PROVIDER=fake`; re-run `make demo` |
| `login failed ... invalid credentials` | Use tenant `demo` and the password printed by the first seed run |
