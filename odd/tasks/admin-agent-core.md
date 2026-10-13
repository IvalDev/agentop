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
- [ ] T3 — Identidad: modelos User/Tenant/Role/Permission, registro de usuarios, login JWT, middleware de tenant, RBAC dependency
- [ ] T4 — Almacenamiento: modelos Document/DocumentChunk, subida con validación, extracción de texto (PDF/DOCX/TXT/MD), worker de ingesta
- [ ] T5 — Embeddings y retrieval: adaptador de embeddings, indexación pgvector, endpoint de búsqueda semántica + híbrida, scoring
- [ ] T6 — Motor de herramientas: registry de herramientas tipadas con permisos y política de aprobación, ejecución acotada, dry-run
- [ ] T7 — Aprobaciones: modelos ApprovalRequest, flujo request→approve/reject con binding exacto, TTL, execute_approved_action
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

## Rationale de cambios aceptados

- (pendiente)
