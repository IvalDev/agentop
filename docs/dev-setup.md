# Light development setup (no VPS, weak machine)

Run the whole agent on a modest laptop against a **fictional demo company**: one SQLite
file instead of Postgres, the chat model hosted by xAI (Grok) instead of local inference,
and no embedding model at all (or a single small one). You get a seeded team with one user
per role, 29 synthetic Spanish business documents about the company and its staff, a
console chat client, and a browser console.

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

   Or use the [web app](#web-app) (`make app`, then <http://localhost:8000/>), or the
   [browser console](#browser-console-development-only) at <http://localhost:8000/ui/>.

Questions to try (the documents are in Spanish):

- ¿Qué facturas de proveedores están pendientes de pago y cuándo vencen?
- ¿Quién tiene que aprobar la factura TIS-2026-0150 según el procedimiento interno?
- ¿Cuánto pagamos al mes por la oficina, IVA incluido?
- ¿Qué reclama Comercial Ejemplo Levante sobre la factura V-2026-0031?
- ¿Cuántas personas trabajan en la empresa y quién dirige cada departamento?
- ¿Quién lleva la contabilidad y cuál es su extensión?
- ¿Dónde está el almacén y en qué horario se recibe la mercancía?
- ¿A quién pido las vacaciones y con cuánta antelación?

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
| `--no-documents` | Creates only the tenant and team: an empty knowledge base to fill through the console or the API. |

It creates the tenant **Suministros Ficticios Demo, S.L.** (slug `demo`), its team, and the
29 documents in `app/demo/fixtures/`, ingested through the real documents service with the
owner as the acting user, so chunks, citation offsets, embeddings, and audit events are real:

| Prefix | Count | Documents |
| --- | --- | --- |
| `empresa_` | 7 | General data, statutes, sites and addresses, org chart and staff directory, main clients and suppliers, product catalogue, internal FAQ |
| `empleado_` | 8 | One record per employee, including the four demo users |
| `rrhh_` | 3 | Welcome manual, leave policy, 2026 work calendar |
| `factura_`, `contrato_`, `correo_`, `procedimiento_` | 5, 2, 3, 1 | Invoices, contracts, emails, and the internal payment procedure |

Employee records deliberately omit sensitive personal data (ID numbers, salary, bank
accounts, home address, personal phone, date of birth, health or family data): documents
have no per-role permissions, so every role, viewer included, can retrieve every document.
`tests/test_demo_seed.py` checks the records for ID numbers, IBANs, and pay amounts.

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
| owner | `owner@demo.test` | `demo-owner-pass` | Everything below, plus manage owners and read the audit log (`GET /api/v1/audit`) |
| admin | `admin@demo.test` | `demo-admin-pass` | Everything below, plus [manage the team](#manage-the-team-owner-and-admin) except owners |
| operator | `operator@demo.test` | `demo-operator-pass` | Chat, search, upload and ingest documents; review approvals for operator-level tools |
| viewer | `viewer@demo.test` | `demo-viewer-pass` | Chat, search, list documents (read-only) |

The agent only offers the tools a role may run, and an approval request can only be seen
and decided by a role that satisfies the tool's required role. Today the only built-in
tool is `search_documents` (viewer and up), so no chat run produces an approval request
yet; the client already renders them for the action tools that come later.

## Manage the team (owner and admin)

Owners and admins manage the users of their own company; every other role gets 403. The
company always comes from the token, never from the request.

| Method and path | Body | Success |
| --- | --- | --- |
| `GET /api/v1/users` | none | 200 `{"users": [user, ...]}`, oldest first |
| `POST /api/v1/users` | `email`, `full_name`, `role`, `password` | 201 user |
| `PUT /api/v1/users/{id}/role` | `role` | 200 user |
| `POST /api/v1/users/{id}/deactivate` | none | 200 user |
| `POST /api/v1/users/{id}/reactivate` | none | 200 user |

A user is `{"id", "email", "full_name", "role", "is_active", "created_at"}`; `role` is
`owner`, `admin`, `operator`, or `viewer`. New emails are trimmed and stored lowercase,
and an initial password needs 8 characters to 72 bytes.

| Answer | When |
| --- | --- |
| 403 `forbidden` | Caller is operator or viewer; or an admin creates an owner, promotes to `owner`, or changes, deactivates, or reactivates an owner |
| 404 `user not found` | The id is unknown or belongs to another company (both answer the same) |
| 409 with the rule as `detail` | `email already registered`, `cannot change your own role`, `cannot deactivate yourself`, `tenant must keep at least one active owner` |
| 422 | Invalid body, or any extra field such as `tenant_id` |

A deactivated user cannot log in, and their tokens stop working on the next request; a
role change also applies on the next request. Setting the role or state a user already
has changes nothing. Every change is audited (`user.created`, `user.role_changed`,
`user.deactivated`, `user.reactivated`) without the password, and refusals by the rules
above are audited as `user.denied`.

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@demo.test","password":"demo-admin-pass","tenant_slug":"demo"}' \
  | jq -r .access_token)
curl -s localhost:8000/api/v1/users -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"email":"new@demo.test","full_name":"New Hire","role":"viewer","password":"change-me-now"}' | jq .
```

## Embeddings on a weak machine

| Option | Settings | Ranking quality |
| --- | --- | --- |
| No model (default in `grok.env.example`) | `AA_EMBEDDING_PROVIDER=fake` | Semantic and hybrid search still run, but the "fake" vectors are hashed words: results rank by shared words, not meaning |
| One small local model | `AA_EMBEDDING_PROVIDER=ollama`, `AA_EMBEDDING_MODEL=nomic-embed-text`, `AA_EMBEDDING_DIM=768`; `ollama pull nomic-embed-text` | Real semantic similarity; Ollama runs no chat model |

Changing the embedding provider or model after seeding mixes incompatible vectors: delete
`dev.db` and `var/storage` and run `make demo` again.

## Web app

The React app in `web/` is the product console: log in, then use **Chat**, **Base de
conocimiento**, and **Configuración → Equipo**. Its interface is in Spain Spanish.
Prerequisites: Node 22.22+ with npm, plus steps 1–3 of the [quick path](#quick-path-sqlite--grok--no-embedding-model).

### Run it

```bash
make app
```

This installs the web dependencies when they are missing or outdated, builds `web/dist`, and
starts the API on <http://localhost:8000/> with the same `.env` handling as `make dev`. Log in
with company `demo` and any user of [the demo team](#the-demo-team), for example
`admin@demo.test` / `demo-admin-pass`.

| Route | Section | Who sees it | State |
| --- | --- | --- | --- |
| `/login` | Iniciar sesión | Everyone | Done: company, email, password |
| `/chat` | Chat | Every role | Placeholder; streaming, citations, and history come in L3 |
| `/conocimiento` | Base de conocimiento | Every role | Placeholder; documents and uploads come in L4 |
| `/configuracion/equipo` | Configuración → Equipo | Owner and admin; other roles are sent to `/chat` | Placeholder; user management comes in L5 |

The header shows your name and role and has **Cerrar sesión**. On a phone-width screen the
navigation collapses behind a **Menú** button.

### Develop with hot reload

```bash
make dev       # terminal 1: the API on :8000
make web-dev   # terminal 2: Vite on http://localhost:5173, forwarding /api to :8000
make web-test  # typecheck, lint (with jsx-a11y), and component tests with axe checks
```

After changing the frontend, `make app` (or `npm --prefix web run build`) rebuilds `web/dist`;
the running API serves the new files without a restart.

### How the API serves it

| Request | Answer |
| --- | --- |
| `/api/...`, `/healthz`, `/docs`, `/redoc`, `/openapi.json`, `/ui` | As before; never the web app |
| A file in `web/dist`, such as `/assets/index-<hash>.js` | The file; hashed assets are cached for a year |
| A missing file (`/assets/...`, or a name with an extension) | 404 JSON, never the page |
| Any other `GET` (`/`, `/chat`, ...) | `index.html`, and the app routes on the client |

The app is mounted in every environment, but only when `web/dist/index.html` exists
(`AA_WEB_DIST_DIR` points elsewhere). Without a build, backend-only development and the test
suite work as before.

### Session and security

- The access token lives in memory and in the tab's `sessionStorage`: a reload keeps you
  logged in, closing the tab logs you out, and it never goes to `localStorage` or cookies.
- The API has no refresh token. After `AA_ACCESS_TOKEN_EXPIRE_MINUTES` (30 by default) any
  request answers 401 and the app returns to the login page with "Tu sesión ha caducado".
  Planned: an httpOnly refresh cookie.
- The email is trimmed and lowercased before login, because the API compares it exactly.
- Server text is always rendered as text, and the app loads no external scripts, fonts, or CDNs.

### Code layout

| Path in `web/src` | Holds |
| --- | --- |
| `i18n/` | `es-ES.ts` catalog (every visible string) and `t()`, `formatDate()`, `formatNumber()` |
| `api/` | `createApiClient` (bearer token, `ApiError` with `detail`, 401 handling) and auth calls |
| `session/` | `SessionProvider`, `useSession()`, roles, token storage |
| `components/` | Presentational atoms, molecules, organisms, and templates |
| `pages/`, `routes/` | Route containers, guards (`RequireAuth`, `RequireRole`), and `paths` |
| `test/` | Fake API, `renderApp()`, and the axe helper |

## Browser console (development only)

With `make dev` running, open <http://localhost:8000/ui/> and log in with tenant `demo`:
as `operator@demo.test` / `demo-operator-pass` to upload, or any user to read and chat.
It is plain HTML + JavaScript over the same API, mounted only when `AA_ENVIRONMENT` is
`dev`, `development`, `local`, or `test`; any other environment answers 404 on `/ui`.

| Panel | What it does |
| --- | --- |
| Login | Tenant slug, email, password; shows the user and role, and logs out |
| Documents | Lists filename, status, type, size, and error message; **Upload & ingest** uploads a file and ingests it in one click, showing the outcome or the API error (upload is hidden for viewers) |
| Search | Runs a search with a mode (`hybrid`, `semantic`, `keyword`) and `top_k`; shows each passage with its document, score, and character offsets |
| Chat | Streams the answer with tool calls, tool results, approval notices, and expandable citations; replays the last 10 messages as history until **Reset conversation** |

The access token lives only in the page's memory: reloading logs you out, and after
`AA_ACCESS_TOKEN_EXPIRE_MINUTES` (30 by default) requests answer HTTP 401 — log in again.

## Walk through the RAG pipeline by hand

Upload a document of your own and follow it through extraction, chunking, embedding,
search, and chat, against the API started by `make dev`. The
[browser console](#browser-console-development-only) does the same with one panel per
step; the commands below use `curl` and `jq`.

> Write facts that appear in no other document (a made-up policy with names and dates),
> so a chat answer about them can only come from your file.

1. Log in as operator or higher (upload and ingest are operator+):

   ```bash
   TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"operator@demo.test","password":"demo-operator-pass","tenant_slug":"demo"}' \
     | jq -r .access_token)
   ```

2. Upload (the trailing slash is required):

   ```bash
   DOC_ID=$(curl -s localhost:8000/api/v1/documents/ -H "Authorization: Bearer $TOKEN" \
     -F "file=@my_document.md" | jq -r .id)
   ```

   `curl` sends no real type (`application/octet-stream`), so the API takes it from the
   extension: `.md`, `.markdown`, `.txt`, `.pdf`, and `.docx` are readable. A type the
   client sends explicitly (`;type=...`) is kept as sent.

3. Ingest: extract the text, chunk it, embed the chunks, and store them (synchronous):

   ```bash
   curl -s -X POST localhost:8000/api/v1/documents/$DOC_ID/ingest \
     -H "Authorization: Bearer $TOKEN" | jq .
   ```

   Expect `"status": "processed"`. A 422 means an unsupported or unreadable file; a 502
   means the embedding backend (Ollama) is unavailable. Both leave the document `failed`.

4. Inspect retrieval without the chat. These are the passages the agent would receive:

   ```bash
   curl -s localhost:8000/api/v1/search -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"query":"words from your document","mode":"semantic","top_k":5}' \
     | jq '.hits[] | {score, document_filename, content}'
   ```

   Compare `semantic`, `keyword`, and `hybrid`. Keyword scoring counts substring matches,
   so a long document that repeats a common word can outrank yours.

5. Ask the chat: run `make chat` (or use the console's Chat panel) and ask something only
   your document answers. The cited passages are listed after the answer.

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
| `http://localhost:8000/ui/` answers 404 | `AA_ENVIRONMENT` is not `dev`, `development`, `local`, or `test`; the console exists only there |
