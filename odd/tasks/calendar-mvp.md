# Feature: calendar-mvp (M2 — Calendario Google)

## Objetivo

Primer módulo real del catálogo: **Calendario con Google**. El admin activa el
módulo para el tenant (ya existe, T5 module-foundations); cada usuario conecta
su propia cuenta de Google con un onboarding guiado (OAuth real); una vez
conectado, el chat puede leer, crear y cancelar eventos del calendario del
usuario. MVP con un solo proveedor; la arquitectura deja preparada la alta de
nuevos providers sin tocar el motor.

## Decisiones (2026-10-15, humano + arquitecto)

- **D1 — Providers: Google solamente en el MVP.** Queda documentado (doc del
  módulo + protocolo de provider) que vendrán más adelante: Exchange/Microsoft
  365 (Graph), CalDAV (iCloud/Nextcloud/generic), calendario de sistema,
  proveedores de terceros o personalizados. El manifest y el adapter viven
  detrás de un `CalendarProvider` con registro por nombre (`google` hoy).
- **D2 — OAuth: authorization-code + PKCE** contra Google
  (`accounts.google.com` + `oauth2.googleapis.com`). Credenciales de la app vía
  env `AA_GOOGLE_CLIENT_ID` / `AA_GOOGLE_CLIENT_SECRET` (SecretStr, requeridas
  solo al usar el flujo). Callback: `GET /api/v1/oauth/google/callback`. El
  `state` es opaco, generado al inicio, guardado server-side en el estado de
  onboarding (con el PKCE verifier) — CSRF + binding usuario×módulo×paso.
- **D3 — Tokens en la conexión cifrada**: el callback escribe
  `UserConnection(provider="google", status="connected")` con el blob Fernet de
  `access_token`, `refresh_token`, `expires_at` y `scopes`. El adapter refresca
  el access token de forma transparente (guarda el nuevo en la conexión).
- **D4 — Scopes fijos, sin scope_select en el MVP**:
  `https://www.googleapis.com/auth/calendar.events` (CRUD de eventos, incluye
  lectura). Un `scope_select` declarativo queda para cuando haya más providers
  o permisos opcionales.
- **D5 — Tools del módulo** (todas `ALLOW_WITHOUT_APPROVAL`: es el calendario
  propio del usuario y el objetivo es fluidez en el chat; el modelo nunca
  autoriza pero aquí la acción es self-service sobre la cuenta del propio
  usuario):
  - `list_events` (rango de fechas, máx. resultados, calendario `primary`)
  - `create_event` (summary, inicio, fin, descripción?, attendees?)
  - `cancel_event` (event_id) — borra el evento del usuario.
  Un usuario sin conexión no ve un 500: el **precheck** de cada tool responde
  `precheck_failed: <motivo accionable>` (en inglés, para el modelo) y el agente
  le pide al usuario ir a Configuración → Herramientas.
- **D6 — Chat**: las tools aparecen en `tool_schemas` cuando el módulo está
  activo para el tenant (gating T2 existente). La conexión es por usuario y se
  resuelve en la ejecución (T3), no en el esquema.
- **D7 — Onboarding del módulo** (manifest declarativo): pasos
  `redirect` ("Conectar con Google") → `test_connection` (lista 1 evento de
  `primary`) → completed. El `test_connection` registra su tester real en
  `connection_testers` (el registro vacío de production se llena con este
  módulo). `restart` destruye la conexión y reinicia el flujo.
- **D8 — UI (web)**: renderer **genérico** de onboarding que interpreta los
  `StepSpec`/`FieldSpec` del manifest (sin ramas por módulo) — es lo que D5 de
  module-foundations pospuso "para cuando venga el calendario". Secciones:
  **Mis herramientas** (por usuario: módulos activos, estado de su conexión,
  CTA al onboarding) y el wizard de onboarding
  (`/configuracion/herramientas/{module}/onboarding`). i18n es-ES. El panel
  admin de T5 no cambia.
- **D9 — Punto de extensibilidad de providers**: el flujo OAuth vive en un
  paquete neutral `app/modules/oauth/` (router `/api/v1/oauth/...`) con la
  implementación Google dentro; el calendario solo declara el provider y el
  manifest. El correo (M3) reutilizará este paquete. Docstrings del protocolo
  `CalendarProvider` y del registro listan explícitamente los providers
  futuros previstos (D1).

## Alcance

- Backend: `app/modules/calendar/` (manifest, registry, service, provider
  protocol + Google adapter, tools, tester), `app/modules/oauth/` (start +
  callback), settings (client id/secret), `ToolContext.credentials_key`,
  extensibilidad documentada.
- Web: Mis herramientas + renderer genérico de onboarding + i18n + tests.
- Fuera de alcance: CalDAV/Exchange/system calendar (solo documentados),
  scope_select, correo (M3), multi-calendario, webhooks/push de Google,
  refresh programado fuera de request (MVP refresca bajo demanda),
  Alembic (sin migraciones: schema vía create_all; tablas nuevas
  `user_connections`/`onboarding_states` ya existen).

## Setup del humano (prueba real)

Crear un proyecto en Google Cloud Console → OAuth client (Web application),
agregar el redirect URI `http://localhost:8000/api/v1/oauth/google/callback`,
habilitar Google Calendar API, y volcar `AA_GOOGLE_CLIENT_ID` /
`AA_GOOGLE_CLIENT_SECRET` en `.env`. Los tests usan fakes; sin env el flujo
real responde un error claro (no un 500).

## Tareas

- [x] T1 — **OAuth core + manifest + provider Google** — commit `d800cdf`;
      RED→GREEN por grupos (config/manifest 12, oauth 23, provider 9, tester 8
      = 52 tests nuevos), spot check parent: ruff/format ✓ + 73 enfocados ✓;
      suite completa `695 passed`; RDD: assess pendiente. Contenido:
      `app/modules/oauth/` — start `GET /api/v1/oauth/{module_key}/authorize`
      (401→501 sin env→404 módulo→400 sin redirect/scopes→302 a Google con
      state one-shot + PKCE) y `GET /api/v1/oauth/google/callback` (state
      opaco in-memory 600 s de un solo uso que transporta el principal
      completo — el callback es tráfico de browser sin bearer —, exchange
      con seam monkeypcheable, errores 400/502 mapeados, persistencia vía
      `connections.service.create_or_update` cifrada, 302 a
      `/configuracion/herramientas`); `app/modules/calendar/` —
      `CALENDAR_MANIFEST` (scope `calendar.events`, tools
      `list_events/create_event/cancel_event`, pasos `connect_google` +
      `test_connection`), protocolo `CalendarProvider` con providers
      futuros documentados (D1/D9: Exchange, CalDAV, system, third-party),
      `GoogleCalendarProvider` con `list_events` real, registry de
      factories por usuario, tester fail-closed (chequeo de token antes de
      buscar provider); settings `AA_GOOGLE_CLIENT_ID/SECRET`;
      `app/main.py` cablea solo el oauth router. **Desvío decidido por el
      humano (opción 2):** nada registra en `default_catalog` en el import
      de producción (allowlist `test_catalog_registry.py` intacta) —
      `GET /api/v1/modules` no lista `calendar` y el admin no puede
      activarlo hasta un cambio revisado del allowlist; los entry points
      (`register_default_calendar` etc.) existen como código tras llamadas
      explícitas. RDD: assess `medium` (`slice_budget_reached`, 69 archivos,
      8246 líneas, base `6b1ae08`), consentimiento: `declined_this_candidate`.
- [x] T2 — **Onboarding integrado** — commit `b666edc`; RED 8 failed + 25
      passed → GREEN 60 enfocados; spot check parent: ruff/format ✓ + 81 ✓;
      suite completa `708 passed`; RDD: assess pendiente. Contenido:
      `complete_redirect_step` en el motor (solo el callback OAuth lo llama;
      el POST genérico del paso redirect sigue respondiendo 501), callback
      best-effort (conexión almacenada sobrevive a errores del motor) con
      landing en `/configuracion/herramientas/{module}/onboarding` (404
      hasta T4, documentado), **puente de credenciales**: `_advance` mezcla
      los secretos de la `UserConnection` más nueva bajo los del estado
      ({\*\*connection, \*\*state}) en pasos redirect/test_connection —
      sin esto el tester vería vacío y el write final habría destruido el
      blob OAuth (bug encontrado y cubierto); provider fallback a la fila
      de conexión (si no, `calendar` ≠ `google` y el tester fallaba);
      `restart` revoca la conexión (`revoke`: status + destrucción de
      credenciales, fila estable para reactivar) solo en flujos con
      paso redirect. Desvío del allowlist sigue vigente (sin cambios en
      producción registrada).
- [x] T3 — **Tools + ToolContext + chat** — commit `b0a3466`; RED por fases
      (19+collection+4 failed) → GREEN; spot check parent: ruff/format ✓ +
      171 enfocados (allowlists bloqueados intactos); suite completa
      `773 passed`; RDD: assess pendiente. Contenido: `app/modules/calendar/
      tools.py` — `list_events` (EXTRACT/viewer), `create_event` y
      `cancel_event` (ACTION/operator, **`ALLOW_WITHOUT_APPROVAL`**: nuevo
      valor en `ApprovalPolicy` porque el invariante ACTION+REQUIRE era
      bloqueante para D5; ACTION+AUTO sigue rechazado), precheck común
      (`not configured`/`expired` en inglés accionable, jamás 500 ni fuga
      de secretos) + refresh bajo demanda (60 s de skew, persiste vía
      `create_or_update` — para eso construye un catálogo descartable de
      un módulo, ya que el default no registra `calendar`); entry point
      `register_calendar_tools` (registro de producción intacto, decisión
      T1); provider Google con `create_event`/`delete_event` reales +
      rango `time_min/time_max` en `list_events`; oauth
      `refresh_access_token` (preserva el refresh_token guardado — Google
      no lo devuelve en el refresh); `ToolContext` suma `credentials_key` y
      `settings` (deviación documentada: el refresh necesita client
      id/secret de Settings) cableados `execute_tool` → `AgentDeps` →
      router (la ruta de aprobaciones queda con default None: los tools
      calendario nunca pasan por ahí); D6 verificado: `tool_schemas`
      incluye las 3 tools con el módulo activo y las excluye sin él.
      RDD: assess `medium` (`slice_budget_reached`, 73 archivos, 11165
      líneas, base `6b1ae08`), consentimiento: `declined_this_candidate`.
- [x] T4 — **Web: Mis herramientas + onboarding genérico** — commit
      `87dc864`; RED 11 failed → GREEN 35 enfocados; spot check parent:
      typecheck ✓, lint ✓, **129 web tests** ✓, build ✓; RDD: assess
      pendiente. Contenido: `paths.myTools` + wizard en
      `/configuracion/herramientas/:module/onboarding` (el 302 del callback
      aterriza ahí), rutas para cualquier rol autenticado (el grupo de
      settings siempre visible; Equipo/Herramientas conservan su regla
      admin vía prop renombrada `showAdminSections`), `MyToolsPage`
      (módulos activos + estado por `GET /onboarding/{key}` — sin
      `GET /connections`, justificado: completed implica conexión —,
      estados loading/error/empty, CTA "Configurar"), `OnboardingPage`
      renderer cero-ramas por módulo: redirect → `window.location.assign`
      al authorize (URL exacta asertada con `vi.stubGlobal('location')`),
      test_connection sin answers, form con errores 422 inline por field
      key (secret → password), scope_select, completed con restart tras
      `ConfirmPanel`, salidas amables 404/409; i18n es-ES neutro.
      **Nota:** el título/descripción de los pasos sale del server (el
      manifest del calendario lleva strings de developer en inglés por ser
      artefacto de código — D8 prioriza ramas cero en el renderer);
      cleanup: cachés `node_modules`/`vitest` accidentales en la raíz
      eliminados (sin package.json raíz). RDD: assess `medium` (`slice_budget_reached`,
      78 archivos, 12597 líneas, base `6b1ae08`), consentimiento:
      `declined_this_candidate`.

- [x] T5 — **Allowlist de producción (decisión del humano, 2026-10-15)** —
      commit `e21d6c4`. Opción 1 aprobada: revierte el desvío de T1 (opción 2)
      y habilita la prueba end-to-end real. Cambios: `MODULE_CATALOG_ALLOWLIST
      = ["calendar"]` y `BUILTIN_TOOL_ALLOWLIST = {search_documents,
      list_events, create_event, cancel_event}`; `app/main.py` registra al
      importar `register_default_calendar()`, `register_google_provider()`,
      `register_calendar_tester()` y `register_calendar_tools(default_registry)`
      (una sola vez; segundo registro = `ModuleRegistrationError`, idempotencia
      documentada). Tests que codificaban la decisión vieja invertidos
      (`test_calendar_manifest.py` exige presencia en producción + rechazo de
      registro duplicado, `test_agent_api.py` subproceso imprime las 4 tools,
      cabeceras de `test_catalog_registry`, `test_calendar_tester`,
      `test_catalog_api`, comentario de `test_onboarding_api`). TDD: RED
      3 failed → GREEN, docstrings obsoletos actualizados; ruff check/format
      ✓; **suite completa 773 passed** (5:29). RDD: assess `medium`
      (`slice_budget_reached`, 79 archivos, 12643 líneas, base `6b1ae08`),
      consentimiento: `declined_this_candidate`.

- [x] **BUG — `database is locked` al activar (2026-10-15, prueba manual
      del humano)** — fix `270aaab`. `persist_event` abría una conexión
      separada del pool para comitear la auditoría mientras la sesión de la
      request tenía la fila flushada (transacción de escritura abierta) →
      deadlock de dos escritores en SQLite file (`sqlite3.OperationalError:
      database is locked` en `POST /api/v1/modules/calendar/activate`).
      Ciego para los tests: conftest usa `:memory:` (conexión única
      compartida), donde dos sesiones nunca compiten — reproducido y
      confirmado con script. Arreglo: los servicios de **acción** (catalog
      `activate`/`deactivate`, connections `create_or_update`/`revoke`/
      `delete`/`replace`, onboarding `_advance`/`restart`) pasan a
      `stage_event` (evento en la transacción del caller — ya comiteaban
      justo después → atomicidad acción+auditoría y un solo escritor);
      `persist_event` conserva su contrato independiente para negativas y
      auditorías en vuelo (refuse, upload_rejected, executor, agent) y su
      docstring ahora documenta la guarda SQLite. TDD: RED con test
      file-DB nuevo `tests/test_sqlite_file_db_locks.py` (2 failed con el
      error exacto) → GREEN. **Suite completa 775 passed** (773 + 2).
      Riesgo latente documentado: si un tool futuro usa aprobaciones
      (flush de `approval_request` antes del audit independiente del
      executor), repetiría el patrón — hoy ningún tool production pide
      aprobación.

- [x] **BUG — 401 en `GET /oauth/calendar/authorize` (2026-10-15, prueba
      manual del humano)** — fix pendiente de commit de docs. Síntoma: con
      cualquier usuario, el botón "Conectar con Google" devolvía 401
      `not authenticated`. Causa raíz verificada: la auth web es **solo
      bearer header** en el fetch de `ApiClient`; el wizard navegaba con
      `window.location.assign` (navegación de navegador = sin header) →
      401. Arreglo: nueva ruta `POST /api/v1/oauth/{module_key}/authorize`
      (mismos prechecks vía helper compartido `_authorization_url`, 200
      `{"url"}`) que el frontend llama con fetch autenticado y recién ahí
      navega; el contrato GET 302 queda intacto (docstring tabla
      actualizado). Errores 501/404/400 del start reutilizan el
      `submitFailure`/`notice` existente (sin i18n nuevo). TDD delegado a
      writer: RED backend `assert 405 == 401` (ruta inexistente) + RED web
      (aserción vieja de assign síncrono) → GREEN; spot check parent: 34
      passed + superficies exactas (5 archivos, 197+/13-); writer: ruff ✓,
      44 enfocados ✓, typecheck ✓, lint ✓, **130 web tests** ✓.

## Criterios de aceptación

1. Admin activa "Calendario" en Herramientas → usuario ve Mis herramientas con
   el CTA; sin activación, no ve nada nuevo.
2. Onboarding Google completo: start → consent Google → callback → test de
   conexión → completed; tokens cifrados, jamás en claro/auditoría/respuesta.
3. `restart` borra la conexión y permite repetir el flujo.
4. Chat (con módulo activo y conexión): el agente lista eventos, crea una cita
   y la cancela; sin conexión, el precheck guía al usuario al onboarding.
5. Ruff/format limpios; pytest backend verde; web typecheck/lint/test/build
   verdes; sin secrets reales en el repo.

## Delivery

Estrategia heredada de D4: **stacked-to-main** sobre
`feat/module-foundations`. Commits work-unit por tarea (feat + tests + docs
juntos). RDD ON: assess por commit; consentimiento del humano por slice.

## Progress

- 2026-10-15: doc creado; branch `feat/calendar-mvp` sobre
  `feat/module-foundations`.
- 2026-10-15: T1 completa (`d800cdf`), T2 (`b666edc`), T3 (`b0a3466`),
  T4 (`87dc864`) — evidence arriba. Desvío del allowlist registrado arriba
  (opción 2 del humano) **revocado** por decisión del humano el 2026-10-15
  (opción 1 → T5 `e21d6c4`): el calendario ya está registrado en producción.

## Next step

Feature implementada, cableada en producción; T5 cerrado (`declined_this_candidate`);
bug de activación en SQLite file corregido (`270aaab`). Prueba end-to-end con
cuenta Google del humano — checklist:
`make app`; `.env` con `AA_GOOGLE_CLIENT_ID/SECRET` (consola Google → OAuth
web → redirect `http://localhost:8000/api/v1/oauth/google/callback` →
Google Calendar API habilitada); activar Calendario en Herramientas;
wizard de onboarding; pedirle al chat "listar mis próximos eventos".
