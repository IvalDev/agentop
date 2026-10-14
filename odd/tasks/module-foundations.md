# Feature: Cimientos de módulos (module-foundations)

**Creado:** 2026-10-14 · **Estado:** autorizada (pedido del humano 2026-10-14: "vamos a sentar bien los cimientos")
**Rama:** `feat/module-foundations` (apilada sobre `feat/local-app`)
**Relación:** implementa P3 + P4 + P5 de `odd/tasks/platform-modules.md`. Ninguna herramienta real (M1–M5) entra en esta feature.

## Objetivo

Sentar la mecánica de activación y configuración de herramientas del agente:

- El **admin de un tenant activa módulos** desde su panel (Configuración).
- Los módulos activos **están disponibles para todos los usuarios** del tenant.
- **Cada usuario configura sus conexiones** mediante un onboarding propio.
- Las **credenciales quedan cifradas** en reposo y nunca llegan a la API, los logs, la auditoría ni el modelo.
- El **agente solo ofrece y ejecuta herramientas de módulos activos** (además del filtro de rol que ya existe).

## Problema / por qué

Hoy el registro de herramientas es global y el único filtro es el rol: cualquier tool registrada aparece en el chat de todos los tenants. No existe dónde guardar credenciales de proveedores (Google, CalDAV…), ni estado de onboarding por usuario, ni un panel donde el admin active capacidades. Sin estos cimientos, cada módulo (calendario, correo…) tendría que reinventar activación, secretos y configuración.

## Decisiones del humano (2026-10-14)

1. **Modelo de activación/configuración:** admin activa el módulo para el tenant; cada usuario lo configura por su cuenta (conexión con titular). Encaja con D5 de `platform-modules`.
2. **Alcance de esta feature:** solo cimientos. "No montemos aún ninguna herramienta."
3. **UI en esta tajada:** sí el **panel de activación** del admin en Configuración; **no** el renderizador genérico de onboarding (llega con el calendario, cuando haya pasos reales que validar).
4. **Entrega (D4, ya decidido):** `ask-on-risk` con cadena `stacked-to-main`; fronteras de slices registradas abajo.

## Alcance

### En alcance

- **Catálogo de módulos**: manifiestos declarados en código (registro tipado con invariantes, patrón hermano de `ToolRegistry`).
- **Activación por tenant**: modelo + API solo-admin + auditoría.
- **Gating de herramientas**: `ToolSpec` declara su módulo; el agente no ofrece ni ejecuta herramientas de módulos inactivos; las herramientas core (`module=None`) siguen siempre disponibles.
- **Conexiones por usuario**: modelo con credenciales cifradas (Fernet, clave desde settings, fuera de la DB), API de usuario (nunca devuelve secretos) y listado/revoke para admin (solo metadatos, según D5).
- **Motor de onboarding**: estado por usuario×módulo reanudable, pasos declarativos por el manifiesto (form, redirect, scope_select, test_connection), los campos `secret` se cifran al connection y no se guardan en claro en el estado.
- **UI**: Configuración → Herramientas (activar/desactivar, estado por módulo, empty state), owner/admin, i18n `es-ES`, accesibilidad axe.

### Fuera de alcance

- Herramientas reales de calendario/correo/facturas (M1–M5) y cualquier conector OAuth real.
- Renderizador genérico de onboarding en la UI (decisión 3).
- Automatismos/worker (P6), "Mis conexiones" como sección de UI para todos los roles (P8 — la API de conexiones sí existe, propia del usuario).
- Migraciones Alembic (sigue el patrón del repo: `Base.metadata.create_all`; `alembic/versions` está vacío).

## Criterios de aceptación

1. El admin activa y desactiva módulos de su tenant; otros tenants quedan aislados; no-admin recibe 403; cada cambio queda auditado sin secretos.
2. El agente no ofrece ni ejecuta una herramienta de módulo inactivo para el tenant; las herramientas core nunca se apagan.
3. Las credenciales se guardan cifradas y no aparecen en respuestas de API, logs, auditoría ni mensajes al modelo (verificado por tests).
4. El onboarding por usuario×módulo es reanudable; los campos secretos jamás se persisten en claro; completarlo crea/actualiza la conexión a `connected`.
5. La UI de activación es accesible (axe) y visible solo para owner/admin.
6. `ruff check . && ruff format --check . && pytest -q` y (web) `typecheck && lint && test && build` en verde por work unit.

## Checks por tarea

- TDD: RED observado antes de implementar (fixture/runner existentes; sin runner → excepción explicada).
- Tests de aislamiento entre tenants y de no-fuga de credenciales en toda tarea que toque conexiones.
- Conectores/engine probados con dobles; nunca credenciales reales.
- Verificación por work unit: `ruff check . && ruff format --check . && pytest -q`; web: `npm --prefix web run typecheck && npm --prefix web run lint && npm --prefix web test && npm --prefix web run build`.

## Checklist de tareas

- [x] T1 — **Catálogo de módulos y activación por tenant** (backend): commits `7ddd32f` (doc) + `a91e7ad` (feat); RED 2 errores de colección (`ModuleNotFoundError app.modules.catalog`) → GREEN 32 tests nuevos; ruff ✓, format ✓, pytest 578 passed (spot check del parent re-ejecutado ✓); RDD: assess `medium` (`slice_budget_reached`, 12 archivos, 1174 líneas, base `6b1ae08`), consentimiento del humano: `declined_this_candidate`. Ruta: delegated direct (writer; trigger: 2+ archivos no triviales). Detalles: `app/modules/catalog/` (`ModuleManifest`/`StepSpec`/`FieldSpec` data-only, `ModuleCatalog` con invariantes, `default_catalog` vacío con test allowlist, `ModuleActivation` unique `(tenant_id, module_key)`, servicio auditado, router `GET /api/v1/modules` + `POST|DELETE /{key}/activate` idempotentes owner/admin); catálogo servido desde `app.state.module_catalog`; contratos en `packages/contracts/schemas.py`; `active_module_keys(session, tenant_id)` queda como integración para T2. **Nota:** `DELETE` apunta a `/activate` (la activación es el sub-recurso que se elimina); pasos de onboarding no se exponen en GET (vienen en T4)
- [ ] T2 — **Gating de herramientas por módulo activo**: campo `ToolSpec.module: str | None` (None = core, siempre activa); helper `active_module_keys(session, tenant_id)`; el filtro de oferta en `tool_schemas` (hoy solo rol — `app/modules/agent/service.py:140`) pasa a rol + módulo activo; gate en el executor (`execute_tool` con parámetro explícito `active_modules`, default `None` = sin gate para no romper tests existentes) invocado desde el agente y desde la ejecución de aprobaciones; evento de auditoría dedicado para la denegación; tests: herramienta de módulo inactiva → ni ofrecida ni ejecutable, core intacta. **Ruta: delegated direct (writer).**
- [ ] T3 — **Conexiones por usuario con credenciales cifradas**: dependencia `cryptography` en `pyproject.toml`; `app/core/crypto.py` (Fernet con clave de settings `credentials_key`, obligatoria en producción, validación al arranque, `SecretStr`); modelo `UserConnection(tenant_id, user_id, module_key, provider, status ∈ pending|connected|expired|revoked|failed, credentials_encrypted, metadata JSON, last_health_at)` único por `(user, module, provider)`; servicio + router: usuario gestiona las suyas (create/update/list/get/delete-propia), admin lista metadatos del tenant y revoca; nunca devuelve secretos; auditoría sin secretos; tests: round-trip de cifrado, no-fuga en API/logs/auditoría, aislamiento usuario/tenant, transiciones de estado. **Ruta: delegated direct (writer).**
- [ ] T4 — **Motor de onboarding**: modelo `OnboardingState(tenant_id, user_id, module_key, status ∈ not_started|in_progress|completed, current_step, data JSON sin secretos)`; `StepSpec` declarativos en el manifiesto (`form`, `redirect`, `scope_select`, `test_connection`); servicio get/submit con validación contra el manifiesto, avance, retroceso y reanudación; los campos marcados `secret` se cifran hacia la conexión y jamás se persisten en claro en `data`; al completar → conexión `connected`; router `GET/POST /api/v1/onboarding/{module}`; tests con módulo fixture: reanudación, paso inválido, secreto no persistido en claro, finalización crea conexión. **Ruta: delegated direct (writer).**
- [ ] T5 — **UI: Configuración → Herramientas**: ruta `/configuracion/herramientas` (paths.ts), `RequireRole` owner/admin + entrada en `MainNav` (mismo patrón que Equipo), página con listado del catálogo, activar/desactivar con confirmación y estado, empty state ("aún no hay módulos disponibles"), textos en `web/src/i18n/es-ES.ts`, tests con `installFakeApi` + `expectNoAxeViolations`. **Ruta: delegated direct (writer web).**

## Forecast de entrega

~2.000 líneas autorizadas (add+del) en 5 work units. Estrategia (D4, decidido): **stacked-to-main**. Fronteras de slices: T1, T2, T3, T4, T5 = un commit de work unit cada uno; los PRs se abren solo cuando lo pida el humano.

## Progress / evidence

- 2026-10-14: documento creado; decisiones 1–4 del humano registradas; mapa de código existente hecho (explorer): `tool_schemas` filtra solo por rol (`app/modules/agent/service.py:140-157`), routers se registran en `app/main.py:188-195`, guard de roles `require_roles` (`app/modules/identity/deps.py:91`), alembic sin revisiones (schema por `create_all`), `cryptography` no instalado, panel de Configuración con patrón `RequireRole`+`MainNav`+i18n+`installFakeApi`.
- 2026-10-14: T1 completa (evidence arriba). Siguiente: T2.

## Next step

T2 — gating de herramientas por módulo activo.
