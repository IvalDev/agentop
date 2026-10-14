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
- [x] T2 — **Gating de herramientas por módulo activo**: commit `6c3cf77`; RED 17 failed (`TypeError: unexpected keyword 'module'/'active_modules'`, 146 pre-existentes verdes) → GREEN 163 en los 5 ficheros, ruff ✓, format ✓, pytest 595 passed (spot check del parent ✓); RDD: assess `medium` (`slice_budget_reached`, base `6b1ae08`), consentimiento: `declined_this_candidate`. Ruta: delegated direct (writer). Detalles: `ToolSpec.module: str | None` validado con el mismo patrón que los nombres de tool (None = core); gate en el executor tras rol y antes de validación (`active_modules=None` = sin gate, back-compat) con código `inactive_module` y evento `tool.inactive_module`; `tool_schemas(registry, user, active_modules)` requiere rol Y módulo activo; el set se calcula una vez por run (`run_agent` y SSE router) y otra en `execute_approved_action` antes del claim (aprobación de módulo inactiva → `FAILED` + `execution_error="inactive_module"`). **Riesgos aceptados:** `create_approval_request` no valida activación (solo ejecución; el agente no puede llegar: ni ofrece ni ejecuta); `ToolSpec.module` no se cross-checkea contra el catálogo (key mal escrita → nunca activa)
- [x] T3 — **Conexiones por usuario con credenciales cifradas**: commits `e400d6b` (docs T2) + `7046c59` (feat); RED 3 errores de colección (`ModuleNotFoundError app.core.crypto`) → GREEN 35 focused + 626 full; ruff ✓, format ✓ (spot check del parent ✓); RDD: assess `medium` con desvíos por el `.swp` untracked del editor (resuelto con `--untracked-scope=exclude` + selección explícita vacía), consentimiento: `declined_this_candidate`. Ruta: delegated direct (writer) + interacción autorizada por el humano (opción 1): superficie ampliada a `tests/test_ui.py`, `tests/test_web_app.py`, `tests/test_demo_seed.py` (1 línea `AA_CREDENTIALS_KEY` cada uno, patrón espejo de `AA_JWT_SECRET`) porque la regla de fail-fast en producción rompía 8 tests que fijaban `AA_ENVIRONMENT=production` sin clave. Detalles: `app/core/crypto.py` (Fernet, `CredentialsKeyFormatError` no-ValueError para que Pydantic no evoque la clave, `DecryptionError` con mensaje fijo); settings `credentials_key: SecretStr` + validación de formato eager + obligatoria en producción; `UserConnection` unique `(tenant, user, module, provider)` con `ConnectionStatus`; servicio que cifra y **nunca descifra** para responder, auditoría limitada a `{module_key, provider, status}`; router owner CRUD propio + `GET /tenant` y `DELETE /{id}/revoke` admin; revoke destruye el blob. **Nota:** `cryptography` instalado ad-hoc en `.venv` (pyproject actualizado; no hay lockfile en el repo); clave de test en los 3 ficheros es fixture, no secreto
- [x] T4 — **Motor de onboarding**: commit `a534fda`; RED 1 error de colección (`ModuleNotFoundError app.modules.onboarding`) → GREEN 24 tests, ruff ✓, format ✓, pytest 650 passed (spot check del parent ✓); RDD: assess pendiente. Ruta: delegated direct (writer). Detalles: `OnboardingState` unique `(tenant, user, module)` con `data` JSON sin secretos + columna `secrets_encrypted` (Fernet); `get_state` virtual `not_started` sin mutar, `submit_step` estricto (`extra=forbid` a nivel de campos, required, tipos acotados, scope_select validado contra `manifest.permissions`); `password`-type = secret siempre (defensa en profundidad); `test_connection` vía registry `connection_testers` (vacío en producción; sin tester → 501); `redirect` → 501 (flujo OAuth real viene con el calendario); `restart` auditado; provider = respuesta al campo literal `provider` o `module_key`; al completar: connection `connected` ANTES de marcar completed (reanudable si falla la escritura); auditoría `onboarding.{started,step_completed,completed,restarted}` sin valores. **Desviación aceptada (writer + parent):** secretos intermedios cifrados en columna propia en vez de memoria (resume multi-request lo exige); se limpian al completar/reiniciar. **Riesgos:** `FieldSpec.options` sin invariante de registro (engine valida solo si hay opciones declaradas); registry de testers process-local (MVP single-process)
- [x] T5 — **UI: Configuración → Herramientas**: commit `2eed4ed`; RED 11 failed/12 passed → GREEN 110 web tests; typecheck ✓, lint ✓, build ✓ (spot check del parent ✓); RDD: assess pendiente. Ruta: delegated direct (writer web). Detalles: `web/src/api/modules.ts` (DELETE vía `api.request` porque el cliente no expone `delete`); `paths.tools='/configuracion/herramientas'` + `RequireRole` dentro de la rama de settings; gate renombrado `TEAM_MANAGER_ROLES`→`SETTINGS_ADMIN_ROLES` y prop `showTeam`→`showSettings` (una sola regla owner/admin para Equipo + Herramientas); `ToolsPage` con `ConfirmPanel` accesible (no `window.confirm`), estados loading/error+retry/empty, no-optimista (el estado sale de la respuesta del server), busy por fila; catálogo i18n `tools.*` en es-ES neutro; tests: 7 de página (incl. axe con timeout 15 s) + rutas/nav. **Nota:** el catálogo de producción está vacío hoy, así que la lista se exercita con fixtures hasta que llegue el primer módulo (calendario)

## Forecast de entrega

~2.000 líneas autorizadas (add+del) en 5 work units. Estrategia (D4, decidido): **stacked-to-main**. Fronteras de slices: T1, T2, T3, T4, T5 = un commit de work unit cada uno; los PRs se abren solo cuando lo pida el humano.

## Progress / evidence

- 2026-10-14: documento creado; decisiones 1–4 del humano registradas; mapa de código existente hecho (explorer): `tool_schemas` filtra solo por rol (`app/modules/agent/service.py:140-157`), routers se registran en `app/main.py:188-195`, guard de roles `require_roles` (`app/modules/identity/deps.py:91`), alembic sin revisiones (schema por `create_all`), `cryptography` no instalado, panel de Configuración con patrón `RequireRole`+`MainNav`+i18n+`installFakeApi`.
- 2026-10-14: T1–T4 completas (evidence arriba).
- 2026-10-14: T5 completa (evidence arriba). **Feature implementada**: backend ruff/pytest 650 + web typecheck/lint/110 tests/build en verde. Pendiente: consent de revisión RDD de T5 y revisión manual del humano en el navegador (panel vacío en producción hasta que exista el primer módulo).

## Next step

Feature cerrada a la espera del humano: (1) consent RDD de T5, (2) prueba manual del panel, (3) siguiente feature: **M2 Calendario** (conectores Google + CalDAV según D3, onboarding con pasos reales que ya puede renderizar la UI).
