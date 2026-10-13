# Feature: Núcleo del agente administrativo (admin-agent-core)

**Creado:** 2026-10-13 · **Estado:** en progreso
**Rama:** `feat/admin-agent-core` (work units secuenciales sobre main)

## Objetivo

Construir el núcleo técnico del agente administrativo privado: un monolito modular en FastAPI que implementa identidad, aislamiento por tenant, base documental + RAG con citas, motor de herramientas tipadas, aprobaciones de acciones sensibles y auditoría. Sobre esta base, cualquier flujo administrativo (facturas, correo, tareas, informes…) se añade como módulo sin tocar el núcleo.

## Problema / Por qué

Las pymes ejecutan tareas administrativas repetitivas dispersas en correo, carpetas y hojas de cálculo. Las soluciones de IA genéricas no dan privacidad, control de permisos ni trazabilidad. El núcleo construye la capa que lo hace vendible: inferencia local privada, acciones acotadas por permisos y aprobación humana verificable.

## Scope (núcleo)

- Autenticación y autorización (JWT local inicial; OIDC/Keycloak es fase posterior).
- Multi-tenant: un aislamiento lógico por cliente (esquema DB, storage prefix, política de acceso).
- Ingesta documental: subida, validación, extracción de texto, chunking, embeddings.
- Retrieval RAG con citas verificables (Postgres + pgvector).
- Orquestador de herramientas tipadas: registro de herramientas con esquema Pydantic, permisos y política de aprobación por herramienta.
- Aprobaciones: flujo request → review → execute con binding exacto a la acción + payload.
- Auditoría: registro append-only de acciones del agente, resultados y aprobaciones.
- Chat API con streaming y respuestas fundamentadas en fuentes.
- Observabilidad básica: logs estructurados + métricas Prometheus.

**Fuera de scope (fases posteriores):** control-plane multi-instalación, OIDC/Keycloak, VPS mode, módulos de negocio (facturación, correo…), actualizaciones firmadas, UI web completa (se usará API + cliente mínimo de consola).

## Decisiones de arquitectura registradas

| # | Decisión | Motivo |
|---|----------|--------|
| A1 | Monolito modular FastAPI + workers de cola (Redis+RQ) | Un solo deploy por cliente; separar solo cuando un componente real lo exija |
| A2 | Python 3.12+ / FastAPI / Pydantic v2 | §4.3 del documento; contratos de E/S tipados |
| A3 | PostgreSQL 16 + pgvector | Metadatos + vectorial en una sola base por tenant |
| A4 | MinIO (S3 compatible) para documentos crudos | Archivos separados de metadatos |
| A5 | Ollama como runtime de inferencia inicial | Despliegue local simple; adaptador abstracto para migrar a vLLM |
| A6 | JWT local con refresh para MVP; OIDC después | Arranque sin dependencia externa |
| A7 | Aprobaciones siempre en backend, binding exacto action+payload | §3.3 y §16: el modelo nunca autoriza |
| A8 | Un esquema de base de datos por tenant en el mismo cluster | Aislamiento lógico verificable sin coste de cluster por cliente |

## Checklist de tareas

- [x] T1 — Scaffold: estructura del repo, pyproject, Docker Compose base, healthcheck, CI mínima (ruff+pytest) — commit `d1b7ea9`; ruff ✓, format ✓, pytest 1 passed, compose config ✓; review: high_risk (CI shell), consent declined_this_candidate
- [x] T2 — Configuración y arranque: settings tipadas, lifespan, logging estructurado, DB session, migraciones Alembic — commit `0e76c06`; ruff ✓, pytest 10 passed, alembic heads ✓; review: lineage review-6ed73f672c118095 stop `unachievable_lens_slot` (transporte de tareas OpenCode rechazó los 4 revisores con `opencode_review_transport_binding_invalid` — sesión iniciada antes del git init; fallo del runtime cliente, no del review Go)
- [x] T3 — Identidad: modelos User/Tenant/Role/Permission, registro de usuarios, login JWT, middleware de tenant, RBAC dependency — commit `464d68f`; ruff ✓, format ✓, pytest 32 passed; review: lineage review-a50f0185a6d03303 stop `unachievable_lens_slot` (los 4 lanzamientos de revisor `review-*` fallaron con el límite free-tier del runtime OpenCode — fallo del proveedor cliente, no del review Go)
- [x] T4 — Almacenamiento: modelos Document/DocumentChunk, subida con validación, extracción de texto (PDF/DOCX/TXT/MD), worker de ingesta — commit `ff95c29`; ruff ✓, format ✓, pytest 60 passed; RDD: medium (slice_budget_reached), consent declined_this_candidate (omisión puntual)
- [x] T5 — Embeddings y retrieval: adaptador de embeddings, indexación pgvector, endpoint de búsqueda semántica + híbrida, scoring — commit `361fa60`; ruff ✓, format ✓, pytest 89 passed; RDD: medium (slice_budget_reached), consent declined_this_candidate (omisión puntual)
- [x] T6 — Motor de herramientas: registry de herramientas tipadas con permisos y política de aprobación, ejecución acotada, dry-run — commit `a7a9de8`; ruff ✓, format ✓, pytest 131 passed; RDD: medium (slice_budget_reached), consent declined_this_candidate (omisión puntual)
- [x] T7 — Aprobaciones: modelos ApprovalRequest, flujo request→approve/reject con binding exacto, TTL, execute_approved_action — commit `fa070e0`; ruff ✓, format ✓, pytest 165 passed; RDD: medium (slice_budget_reached), consent declined_this_candidate (omisión puntual)
- [x] T7.1 — Lectura de aprobaciones restringida: list/get exigen el rol requerido por la herramienta (decisión del humano 2026-10-13; antes cualquier rol del tenant veía payloads) — commit `75f4192`; ruff ✓, format ✓, pytest 169 passed; RDD: medium, `under_budget` (174 líneas, pendiente en el slice desde `2f08b35`)
- [ ] T8 — Auditoría: tabla append-only, hooks en todas las acciones, consulta por tenant/actor/acción
- [ ] T9 — Orquestador LLM: adaptador Ollama, loop de agente con tool-calling, prompt de sistema con política anti-prompt-injection, streaming chat con citas
- [ ] T10 — Integración: chat endpoint end-to-end con RAG + herramientas + aprobación + auditoría sobre fixtures
- [ ] T11 — End-to-end del primer flujo administrativo completo (extracción de factura → aprobación → registro) como prueba viviente
- [ ] T12 — Deployment: docker-compose de producción por cliente, backup/restore, documentación de operaciones

## Authorized scope

- Superficie de escritura: `app/`, `packages/`, `tests/`, `deployment/`, `docs/`, `pyproject.toml`, `README.md`, `Makefile`, `.github/workflows/`
- No tocar: nada fuera del repo.

## Acceptance criteria (núcleo)

1. `docker compose up` levanta API + Postgres + Redis + MinIO + Ollama con una sola instalación.
2. Un usuario autenticado solo ve documentos y auditoría de su tenant.
3. `search_documents` devuelve fragmentos con cita verificable (doc_id + offset).
4. `extract_invoice` extrae campos estructurados sin aprobación; `send_email` requiere aprobación con binding exacto y TTL.
5. Ninguna herramienta de producción expone shell, SQL arbitrario ni filesystem abierto.
6. Toda acción del agente produce una entrada de auditoría con actor, herramienta, payload y resultado.
7. pytest + ruff en verde; cobertura de `app/` ≥ 70% en los módulos del núcleo.

## Checks por tarea

- `ruff check .` y `ruff format --check .`
- `pytest` (unit + integration con Postgres de test o SQLite/pgvector embebido cuando aplique)
- Smoke: `curl localhost:8000/healthz`

## Progress / evidence

- 2026-10-13: feature creada.
- 2026-10-13: T1 completa — commit `d1b7ea9` (feat/admin-agent-core, raíz: 29 archivos, 471 líneas). Verificación: ruff check ✓, ruff format ✓, pytest 1 passed, docker compose config ✓. RDD: assess high_risk (shell en CI workflow); consentimiento del humano: declined_this_candidate (omisión puntual, no desactiva revisiones). Siguiente: T2.
- 2026-10-13: T2 completa — commit `0e76c06` (18 archivos, 575 líneas). Verificación: ruff ✓, format ✓, pytest 10 passed, `alembic heads` vacío sin error. RDD: consentimiento granted → linaje `review-6ed73f672c118095`, 4 lentes (risk/resilience/readability/reliability), corrección congelada 200 líneas. Los 4 relanzamientos de revisor fallaron con `opencode_review_transport_binding_invalid` (runtime OpenCode: la sesión se inició antes del git init, el hook de transporte no puede vincular el contexto del repo). Slots declarados `unachievable` (reason `opencode_transport_binding_mismatch`); ciclo paró en `unachievable_lens_slot`, no transitorio en esta sesión. Entrega: commit en rama feature bajo política ordinaria. **Acción para la próxima sesión: reiniciar OpenCode para que el transporte de review vuelva a vincular; los candidatos futuros de RDD deberían poder revisarse.** Siguiente: T3.
- 2026-10-13: T3 completa — commit `464d68f` (11 archivos nuevos en `app/modules/identity/` + `app/core/deps.py` + contratos de auth en `packages/contracts/schemas.py`, 22 tests nuevos → 32 total). Verificación: ruff ✓, format ✓, pytest 32 passed. Módulo: modelos User/Tenant/Role/Permission (UUID PKs, tenant_id en User), JWT HS256 + refresh, bcrypt (`passlib[bcrypt]`), `get_current_user` / `require_roles` dependencies, `scoped_query()` para aislamiento por tenant, seed idempotente de roles/permisos, `/api/v1/auth/{login,refresh,me}`. Decisiones: login devuelve 401 uniforme (no revela existencia de usuario), roles fijos owner/admin/operator/viewer, `scoped_query` es el único punto de extensión de tenant. RDD: assess `high_risk`; consentimiento granted → linaje `review-a50f0185a6d03303`, 4 lentes, corrección congelada 200. Los 4 lanzamientos de revisor fallaron con `Subagent failed: OpenCode's free tier can only be used from within OpenCode` (runtime OpenCode free-tier — fallo determinístico del proveedor cliente, no del review Go). Slots declarados `unachievable` (reason `opencode_provider_task_rejected`); ciclo paró en `unachievable_lens_slot`. Entrega: commit en rama feature bajo política ordinaria. **Restricción conocida: los candidatos RDD no pueden ejecutar los 4 lentes en este runtime; se necesitará un runtime con subagentes habilitados (pago) para completar revisiones nativas.** Siguiente: T4.
- 2026-10-13: T4 completa — commit `ff95c29` (módulo `app/modules/documents/`: models, storage (protocolo + `LocalDocumentStorage`), extract (TXT/MD/PDF/DOCX), chunking (ventana con overlap, fix de loop terminal), service (`create_document`/`ingest_document`, aislamiento por tenant, commit propio del estado `failed`), worker `run_ingest`, router `/api/v1/documents` con roles operator+; config `max_upload_bytes`/`storage_base_dir`; contratos; 28 tests nuevos → 60 total). Verificación: ruff ✓, format ✓, pytest 60 passed. TDD: RED→GREEN en chunking (loop terminal OOM preexistente corregido), extraction y API (401/201/422/413/tenant-scoped/ingest). RDD: assess `medium` (`slice_budget_reached`, 13 archivos, 992 líneas); consentimiento del humano: `declined_this_candidate` (omisión puntual). Entrega: commit en rama feature bajo política ordinaria. Siguiente: T5.
- 2026-10-13: T5 completa — commit `361fa60` (módulo `app/modules/retrieval/`: embeddings.py con `EmbeddingProvider` protocol + `OllamaEmbeddingProvider` + `FakeEmbeddingProvider` determinista (crc32 buckets + L2), vector_type.py con `EmbeddingVector` TypeDecorator dual (pgvector Vector(768) en Postgres / JSON en SQLite), service.py con búsqueda keyword/semantic/hybrid (RRF k=60, siempre tenant-scoped), router.py `POST /api/v1/search`; chunking extendido a tuplas (text, start_offset, end_offset) para citas verificables; DocumentChunk con `start_offset`/`end_offset` NOT NULL + `embedding` nullable; ingest embebe chunks con provider (rollback→re-SELECT→commit `failed` en fallo); config `embedding_provider/model/dim/timeout_seconds`; contratos SearchRequest/SearchHit/SearchResponse; pgvector+httpx a main deps; conftest autouse `AA_EMBEDDING_PROVIDER=fake`; 29 tests nuevos → 89 total). Verificación: ruff ✓, format ✓, pytest 89 passed. TDD: RED→GREEN en offsets, providers, ingest-embedding, search service y search API. Decisiones: RRF k=60 para hybrid, keyword scoring Python-side (portable SQLite/Postgres), cast explícito `Vector(dim)` para cosine_distance en Postgres (TypeDecorator no proxy comparators). RDD: assess `medium` (`slice_budget_reached`, 17 archivos, 1381 líneas); consentimiento del humano: `declined_this_candidate`. Entrega: commit en rama feature bajo política ordinaria. **Follow-up T12: `CREATE EXTENSION IF NOT EXISTS vector` + migración baseline Alembic.** Siguiente: T6.
- 2026-10-13: T6 completa — commit `a7a9de8` (módulo `app/modules/tools/`: registry.py con `ToolSpec` inmutable (kind EXTRACT/ACTION, `ApprovalPolicy` AUTO/REQUIRE_APPROVAL, invariante ACTION ⇒ REQUIRE_APPROVAL, nombres validados, sin duplicados), executor.py (lookup → rango de rol (menor = más privilegio, bypass superadmin igual que `require_roles`) → validación de input → gate de aprobación → dry-run → handler → validación de output; errores como valores de resultado, `forbidden` sin revelar rol requerido), hook de aprobación para T7: `approval_required` con action key `<tool>:<sha256(tool + input JSON canónico)>` sin invocar el handler; builtin.py con `search_documents` (EXTRACT/AUTO/viewer+, input `extra="forbid"`, query 1–500, top_k 1–20, tenant tomado del usuario); 42 tests nuevos → 131 total). Ruta: delegated direct (writer; trigger: 2+ archivos no triviales + preparación de escritura). Verificación: ruff ✓, format ✓, pytest 131 passed (spot check del parent re-ejecutado ✓). TDD: RED (ImportError `builtin` en colección + 9 F401) → GREEN. Guardas criterio 5: allowlist explícita de herramientas registradas, inputs sin campos `command`/`sql`/`path`/`file_path`, `tenant_id` inyectado → error de validación. RDD: assess `medium` (`slice_budget_reached`, 6 archivos, 1313 líneas), linaje `review-95a0a8af43341cef`; consentimiento del humano: `declined_this_candidate`. **Pendientes para T9:** importar `app.modules.tools.builtin` para poblar `default_registry`; pasar `EmbeddingProvider` a los handlers para búsqueda semantic/hybrid (hoy keyword-only); decidir si la API enmascara `str(exc)` de errores de handler. Siguiente: T7.
- 2026-10-13: T7 completa — commit `fa070e0` (módulo `app/modules/approvals/`: models.py `ApprovalRequest` + `ApprovalStatus` + `UTCDateTime` (SQLite devuelve datetimes naive); service.py create/list/get/approve/reject/`execute_approved_action` con `now` inyectable; grants.py `ApprovalGrant` + `mint_grant`/`consume_grant` (WeakSet de grants vivos, un solo uso, `hmac.compare_digest` del action key); exceptions.py; router.py `/api/v1/approvals` (list/get/approve/reject/execute); executor: `approval_token: str` reemplazado por `approval_grant` verificado, `role_satisfies` público; `main.py` monta router y `app.state.tool_registry = default_registry`; config `approval_ttl_seconds=86400`; contratos `ApprovalResponse`/`ApprovalListResponse`/`ApprovalRejectRequest`; 35 tests nuevos (1 reemplazado) → 165 total). Política aprobada por el humano: aprueba/rechaza cualquier usuario del mismo tenant con rango ≤ rol requerido, incluido el solicitante. Máquina de estados: pending → approved|rejected|expired; approved → executing → executed|failed; approved → expired; todas las transiciones `UPDATE … WHERE id AND status` con rowcount. Binding: action key calculado en el servicio al crear; al ejecutar se re-valida el payload almacenado (canónico exacto) y se recalcula la key; mismatch → `failed` + 409. TTL verificado en approve/reject/execute (→ `expired` + 410). Un solo uso: claim atómico approved→executing commiteado antes del handler; rechazos (rol/TTL/estado/binding) antes del claim. HTTP: 404 otro tenant/inexistente, 403 rol, 409 estado/binding, 410 expirado, 200 con `status: failed` si el handler falla. Ruta: delegated direct (writer; trigger: 2+ archivos no triviales). Verificación: ruff ✓, format ✓, pytest 165 passed (spot check del parent re-ejecutado ✓). TDD: RED (`ModuleNotFoundError app.modules.approvals.grants`, 3 errores de colección) → GREEN; test de concurrencia validado por mutación (sin claim condicional el handler corre 3 veces). RDD: assess `medium` (`slice_budget_reached`, 13 archivos, 1608 líneas), linaje `review-9f4776ea258a621a`; consentimiento del humano: `declined_this_candidate`. **Abiertos:** (1) list/get de aprobaciones abiertos a todo rol del tenant (viewers ven payloads); (2) listado muestra `pending` vencidos hasta que alguien los toca (cliente compara `expires_at`); (3) follow-up de hardening: que approve reciba el `action_key` que vio el revisor (o firmar la key con secreto del servidor) para cubrir manipulación directa de DB. Siguiente: T8.
- 2026-10-13: T7.1 completa — commit `75f4192` (`_readable_roles(user)` vía `role_satisfies`; `list_approval_requests` filtra `required_role IN` en la query; `get_approval_request` 404 por tenant antes de 403 por rol; docstrings de service/router actualizados; fixture `approval_tools` con `queue_note` de nivel operator; 4 tests nuevos → 169). Ruta: delegated direct (writer). TDD: RED 3 failed (viewer veía ambas solicitudes) → GREEN 35 passed en approvals. Verificación: ruff ✓, format ✓, pytest 169 passed (spot check del parent ✓). RDD: assess `medium`, `review_due=false` (`under_budget`, 6 archivos, 174 líneas); queda pendiente en el slice con base `2f08b35`. Siguiente: T8.

## Rationale de cambios aceptados

- T6: `search_documents` como herramienta queda en modo keyword porque el contrato del handler `(session, tenant_id, user, input)` no transporta proveedor de embeddings; se difiere a T9 en vez de acoplar el registry a retrieval.
- T6: `extract_invoice` y `send_email` (criterio 4) no son herramientas de T6; llegan con T11 / módulos de negocio.
- T7: sin regla de cuatro ojos — el solicitante puede aprobar su propia solicitud (pymes con un solo operador). Queda como opción futura por tenant/herramienta.
- T7: el bypass del gate de aprobación pasa de string libre a `ApprovalGrant` acuñado solo por el servicio de aprobaciones; un string o grant construido a mano/reutilizado → `forbidden`.
