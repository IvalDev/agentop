# Feature: Acceso al correo del agente — Gmail (`google-mail`)

**Creado:** 2026-10-18 · **Estado:** alcance definido por el humano (2026-10-18); plan listo — G1 siguiente
**Rama:** `feat/google-mail`, apilada sobre `feat/desktop-widget` (base `17aa632`)
**Módulo:** `google_mail` (clave ya citada como ejemplo válido en `app/modules/catalog/registry.py:11,32`)

## Objetivo

Que el agente pueda operar el correo del usuario con **Gmail (MVP, solo Google)**: el admin habilita el complemento y cada usuario conecta su propia cuenta Google por OAuth, pudiendo elegir el alcance: **todo el buzón o una sola etiqueta** (p. ej. solo la Bandeja de Entrada, o solo la etiqueta «Asistente» que el usuario creó). Dentro de ese alcance el agente **lee libremente**; cualquier **acción la ejecuta solo con la confirmación del usuario** (pedido y decisión del humano, 2026-10-18).

## Por qué ahora

- Pedido del humano 2026-10-18: siguiente herramienta después del widget, con el mismo patrón que el calendario (admin habilita + cada usuario configura su cuenta de Google).
- El framework de módulos genérico ya existe — el calendario es su primera entrada (exploración delegada 2026-10-18): Gmail = paquete de módulo nuevo + manifest + tools, **no** framework nuevo.
- El paso de onboarding `scope_select` ya está implementado y con tests (el calendario no lo usó) → la elección buzón/etiqueta encaja sin mecanismos nuevos.

## Restricciones y decisiones (humano, 2026-10-18)

1. **Proveedor MVP:** solo Google/Gmail.
2. **Activación:** el admin habilita el complemento; cada usuario conecta su cuenta (OAuth propio, mismo flujo que calendario).
3. **Alcance del buzón:** el usuario elige **todo el buzón** o **una sola etiqueta**. Gmail no tiene carpetas: «Bandeja de Entrada» = etiqueta de sistema `INBOX`; «Asistente» = etiqueta de usuario.
4. **Permisos del agente:** lectura libre dentro del alcance + acciones solo con confirmación (mecanismo de aprobación existente del motor).
5. **Enforcement honesto:** Google no concede permisos por etiqueta — el OAuth entrega el buzón completo. La restricción la aplica **nuestro backend** en cada consulta y acción; el agente nunca ve ni toca nada fuera de la etiqueta elegida.

## Decisiones de diseño

- **M1 — Módulo `google_mail`:** manifest (permisos OAuth `gmail.modify` + `gmail.send`; pasos de onboarding `redirect` → `scope_select` → `test_connection`), registro junto al calendario en `app/main.py`, tester = `get_profile` (muestra la cuenta conectada). Patrón de referencia: `app/modules/calendar/` (`manifest.py`, `registry.py`, `testers.py`, `tools.py`).
- **M2 — Elección de alcance:** `scope_select` en onboarding con las etiquetas **reales** del usuario, obtenidas vía API **después del OAuth**: «Todo el buzón», `INBOX`, etiquetas de usuario (nombre + ID; preferir `labelIds` de la API sobre sintaxis de query). La elección persiste accesible en tiempo de ejecución (seguir la convención de persistencia del engine de onboarding). **Incertidumbre clave:** verificar si el `scope_select` del engine acepta opciones dinámicas; si no, extensión mínima del engine es la única superficie ajena autorizada.
- **M3 — Tools** (registro en allowlist revisado como el calendario, comentario de aprobación incluido):
  - **Lectura, sin aprobación** (EXTRACT/viewer): `search_emails` (filtrado por `labelIds` del alcance salvo «todo el buzón»), `get_email` (rechaza con motivo accionable si el mensaje está fuera del alcance), `list_labels` (solo nombres/metadatos).
  - **Acciones, con confirmación** (ACTION/operator): `send_email` (acepta `thread_id` opcional para responder en hilo; **exento del filtro de etiqueta** — un correo nuevo no pertenece a una carpeta — pero SIEMPRE requiere aprobación), `archive_message` (quitar `INBOX`, solo mensajes dentro del alcance), `add_label` (aplicar etiqueta a mensaje dentro del alcance).
  - **Sin borrado en MVP:** nada de trash ni eliminación permanente.
  - Precheck idéntico al calendario: `ToolPrecheckError` accionable (`NOT_CONFIGURED`, `EXPIRED`), sin 500 y sin fugas de tokens.
- **M4 — Web:** el renderer genérico de onboarding pinta los pasos (incluido `scope_select`); entrada en Herramientas y Mis herramientas vía catálogo; claves i18n es-ES nuevas; cambios de frontend solo si G2 los requiere (opciones dinámicas).

## Checklist de tareas

- [ ] G1 — **Módulo base:** manifest (`google_mail`: permisos, pasos `redirect` + `test_connection`, instrucciones de perfil), registro en `app/main.py` + entrada en el catálogo (admin puede activar/desactivar), tester `get_profile`, tests backend RED→GREEN (`tests/test_google_mail_manifest.py`, `tests/test_google_mail_tester.py`, catálogo si aplica). Ruta: delegated direct (writer).
- [ ] G2 — **OAuth + alcance:** verificar que authorize reutiliza los permisos del manifest; tras el callback, obtener las etiquetas del usuario y poblar `scope_select` («Todo el buzón» / `INBOX` / etiquetas de usuario); persistir la elección accesible para las tools; flujo completo de onboarding + `test_connection`; tests (onboarding API/service). Ruta: writer. Depende de G1. **Resuelve acá la incertidumbre de opciones dinámicas.**
- [ ] G3 — **Tools de lectura + acciones con confirmación:** los 6 tools de M3 con precheck, enforcement de alcance (tests de negación obligatorios), allowlist del executor (`tests/test_tools_executor.py`), instrucciones de perfil del manifest actualizadas; RED→GREEN. Ruta: writer. Depende de G2 (elección persistida).
- [ ] G4 — **Web + i18n:** claves es-ES (`tools.google_mail.*`, onboarding), chrome de Herramientas/Mis herramientas/onboarding (renderer genérico), tests web (`make web-test`: typecheck + lint + vitest incl. OnboardingPage/ToolsPage + axe). Ruta: writer. Depende de G2.
- [ ] G5 — **Verificación integral + docs:** suites completas (ruff + pytest + web), `docs/google-mail.md` (guía es-ES admin/usuario: activar complemento, conectar cuenta, elegir alcance, qué puede y qué no puede el agente), protocolo de prueba manual con una cuenta Gmail real (lo ejecuta el humano). Ruta: writer + humana.

Diferidas: otros proveedores (Outlook/Microsoft 365); gestión de etiquetas (crear/editar); borradores; marcar leído/no leído; snooze; adjuntos en envío; elección de **varias** etiquetas (MVP: una sola); edición del alcance tras el onboarding (verificar si re-ejecutarlo reabre `scope_select`; si no, diferir); paginación/orden avanzados de búsqueda.

## Riesgos conocidos

- **`scope_select` dinámico:** puede no existir en el engine → extensión mínima autorizada como única superficie ajena (G2).
- **Enforcement app-side:** Google igual concede el buzón completo; una tool sin filtro sería una fuga. Tests de negación por tool son obligatorios (G3).
- **`send_email` exento de etiqueta:** solo lo controla la aprobación — definido así y testeado.
- **OAuth `pending_states` in-process** (limitación preexistente del framework, documentada).
- **Cuenta real necesaria para E2E:** sin credenciales de prueba en el repo; la prueba manual es del humano (G5).

## Criterios de aceptación (MVP Gmail)

1. El admin activa `google_mail` en `/configuracion/herramientas`; los usuarios lo ven en Mis herramientas.
2. El usuario conecta su Google y elige alcance (todo / `INBOX` / una etiqueta de su lista); el tester muestra la cuenta conectada; la elección persiste.
3. Con alcance «etiqueta X»: el agente **no** puede leer mensajes fuera de X (búsqueda filtrada + `get_email` rechazado con motivo accionable) ni archivar/etiquetar mensajes fuera de X.
4. Toda acción (send/archive/add_label) pasa por el flujo de aprobación; sin confirmación no se ejecuta. La lectura no pide aprobación.
5. Sin conexión: precheck `NOT_CONFIGURED` accionable; ningún 500 ni fuga de tokens.
6. Suites: `ruff check`/`ruff format`/`pytest` OK (incl. allowlist), `make web-test` OK; i18n es-ES sin claves faltantes.
7. `docs/google-mail.md` publicado y protocolo manual ejecutado por el humano con una cuenta real.

## Checks

- Backend: `ruff check . && ruff format --check . && pytest -q`.
- Web: `make web-test` (typecheck + lint + vitest, incluye axe) en toda tarea que lo toque.
- RED→GREEN donde exista runner determinista con resultado esperado claro (pytest); runner: `python -m pytest -q` desde la raíz.
- RDD por candidato según el interruptor; consentimiento del humano por candidato.

## Entrega

- Rama `feat/google-mail` apilada sobre `feat/desktop-widget` (`17aa632`); sin remoto ni `main` → commits work-unit locales, sin push (mismo estado que el resto de la pila).
- Estrategia: `ask-on-risk` (default) hasta que el humano decida otra cosa; previsión ~1.500–2.500 líneas autorizadas (el framework genérico reduce código frente a calendario).
- Sin `Co-Authored-By` ni atribución de IA.

## Progress / evidence

- 2026-10-18 (1): plan creado tras exploración delegada del patrón de calendario (framework genérico confirmado: `ModuleActivation` + OAuth por manifest + `scope_select` existente y probado) y spot check de `app/main.py:44-51`. Alcance cerrado por el humano: lectura libre + acciones con confirmación; elección buzón/etiqueta con enforcement app-side. Rama `feat/google-mail` creada. Próximo: G1.

## Next step

G1 — Módulo base `google_mail` (manifest + registro + catálogo + tester + tests RED→GREEN) vía writer delegado.
