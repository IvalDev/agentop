# Feature: agent-engine-routing (motores por herramienta + contexto horario)

## Objetivo

Que cada herramienta responda con su propio "motor" (solo sus tools y sus
instrucciones) elegido por un router automático, y que el agente conozca la
fecha, la hora y la zona horaria de quien pregunta, registrándolas en la
auditoría.

## Problema (evidencia, prueba manual 2026-10-16)

Auditoría de `dev.db` (runs `ccdddb8b…` y `f1e76f51…`):

- `list_events` de `2023-04-01` a `2023-04-30`: el `SYSTEM_PROMPT` es estático
  y no lleva fecha → el modelo usa la de su entrenamiento.
- `search_documents` antes del calendario: las reglas 5, 6 y 10 del prompt
  obligan a buscar en la base de conocimiento antes de responder cualquier cosa
  (tokens desperdiciados).
- Zona horaria inconsistente: `create_event` con `Z` y luego con `+02:00`; el
  agente no conoce la zona del usuario y Google recibe solo `dateTime` sin
  `timeZone` (`app/modules/calendar/provider.py:163-167`).

## Decisiones (2026-10-16, humano)

- **D1 — Router automático** (opción elegida frente a "selector en el chat" y
  "un agente con prompt modular"): una llamada corta al LLM clasifica cada
  mensaje, con historial reciente, en un motor disponible; el run recibe solo
  las tools y el prompt de ese motor. Tradeoff aceptado: los pedidos que mezclan
  motores no funcionan en un solo mensaje.
- **D2 — Motores**: `knowledge` (núcleo: tools sin módulo, hoy
  `search_documents`, con las reglas de grounding y citas) y uno por módulo
  activo para el tenant que tenga tools visibles para el usuario (hoy
  `calendar`). Las reglas de seguridad 1-4 y la de idioma son comunes a todos.
- **D3 — Perfil de agente en el manifest**: `ModuleManifest.agent` opcional con
  `routing_description` (para el router) e `instructions` (prompt del motor),
  validado en `ModuleCatalog.register`. Sin perfil, se usa la `description`
  del manifest y unas instrucciones genéricas (no se ocultan tools).
- **D4 — Router barato y seguro**: se salta cuando hay un solo motor (sin
  llamada extra); la salida solo puede ser una clave del conjunto cerrado de
  motores; respuesta ambigua o desconocida, o `LLMError` → `knowledge` (modo
  conservador), registrado en la auditoría. El router no amplía permisos: el
  rol y el gating de módulo siguen aplicando en el ejecutor.
- **D5 — Zona horaria desde el navegador**: `ChatRequest.timezone` opcional
  (IANA, p. ej. `Europe/Madrid`), validada con `zoneinfo`; inválida o ausente →
  UTC con `timezone_fallback=true`. El prompt de todos los motores incluye la
  fecha, la hora y la zona local; las tools reciben la zona por `ToolContext`.
- **D6 — Auditoría**: `created_at` ya guarda la hora del servidor en UTC; se
  agregan la zona, la hora local y el fallback al `agent.run_started`, junto
  con el motor elegido y si hubo router o fallback.
- **D7 — Calendario en la zona del usuario**: fechas sin offset se interpretan
  en la zona del usuario; Google recibe `timeZone`.

## Alcance

- Backend: contrato `ChatRequest`, `app/modules/agent/` (prompt, service,
  router y un módulo nuevo de engines/routing), `ToolContext` y `execute_tool`,
  `ModuleManifest` + validación del catálogo, manifest y tools del calendario,
  provider Google.
- Web: `streamChat` manda la zona del navegador.
- Fuera de alcance: `update_event` (propuesto, no autorizado todavía), pedidos
  multi-motor en un solo mensaje, zona horaria por usuario/tenant en la base de
  datos, dependencia `tzdata` (Linux usa la base IANA del sistema; sin ella se
  cae a UTC).

## Tareas

- [x] T1 — **Contexto horario del usuario**: `ChatRequest.timezone`, resolución
      con fallback UTC, línea de fecha/hora/zona en el prompt de sistema,
      campos de auditoría en `agent.run_started`, `ToolContext.timezone`
      cableado `router → AgentRun → execute_tool`; web manda
      `Intl…timeZone`. Ruta: delegada (writer; 2+ archivos no triviales).
      Evidencia: RED 12 failed + 2 errores de colección (módulo inexistente,
      422 `extra_forbidden`, kwargs inexistentes) + 3 web → GREEN; writer:
      ruff ✓, format ✓, 169 enfocados ✓, **suite completa 824 passed**,
      typecheck ✓, lint ✓, **134 web tests** ✓; spot check parent: 102
      enfocados ✓ + ruff ✓ + superficies exactas (15 archivos, 583+/31-).
      Nuevo `app/modules/agent/user_time.py` (`resolve_user_time`,
      `render_time_context`; `ValueError`/`OSError` también caen a UTC). En
      fallback las tools reciben `"UTC"`; la auditoría guarda solo el nombre
      resuelto, nunca el texto crudo del cliente (deliberado, con test).
      Commit `0ca79f2`. RDD: assess `medium` (`slice_budget_reached`, 16
      archivos, 745 líneas), consentimiento: `declined_this_candidate`.
- [x] T2 — **Calendario en la zona del usuario**: entradas sin offset
      localizadas con `ctx.timezone`, `timeZone` hacia Google, descripciones
      de tools, docstrings obsoletos (`tools.py:3-7`, `:467-473`,
      `main.py:210-211`). Ruta: delegada (writer).
      Evidencia: RED 14 failed (mensaje viejo de offset, `KeyError:
      'time_zone'`, kwarg inexistente en el provider) → GREEN 80; writer:
      ruff ✓, format ✓, 107 calendario ✓, **suite completa 842 passed**;
      spot check parent: 80 ✓ + format ✓ + superficies exactas (5 archivos,
      382+/51-). Mezclar horas con y sin offset en un mismo input → error de
      validación accionable; DST con `fold=0`; zona irrecuperable → no se
      manda `time_zone` a Google. Pendientes menores (no bloquean):
      evento que empieza dentro de la hora saltada del cambio de horario y
      termina menos de una hora después llega a Google con el fin antes del
      inicio; quedan textos obsoletos sobre "calendar no registrado en
      producción" en el docstring de `_calendar_catalog`
      (`tools.py`), en el de `tests/test_calendar_tools.py` y en el
      comentario de `connection_testers` de `main.py`. Commit `31e57aa`.
      RDD: assess `medium` sobre T1+T2 (`slice_budget_reached`, 21 archivos,
      1193 líneas, base `225358a`), consentimiento:
      `declined_this_candidate`.
- [x] T3 — **Motores (refactor sin cambio de comportamiento visible)**: prompt
      dividido en base común + grounding de `knowledge`, perfil `agent` en el
      manifest con validación, perfil del calendario, resolución de motores
      disponibles; `AgentRun` acepta un motor (sin motor = comportamiento
      actual). Ruta: delegada (writer).
      Evidencia: RED 4 módulos sin importar (`engines`, `BASE_PROMPT`,
      `AgentProfile` inexistentes) + 2 failed (`AgentDeps.catalog`,
      `ModuleManifest.agent`) → GREEN; writer: ruff ✓, format ✓, 157
      enfocados ✓, **suite completa 878 passed**; spot check parent: 23 ✓ +
      superficies exactas (13 archivos, 801+/29-) + lectura del prompt del
      motor calendario. Nuevo `app/modules/agent/engines.py` (`Engine`,
      `KNOWLEDGE_ENGINE`, `module_engine`, `available_engines`,
      `visible_specs` como único filtro rol + módulo + motor). Única
      diferencia sin motor: la regla de idioma pasa de "11." a línea final
      sin número (compartida por todos los motores). Clave `knowledge`
      reservada y límites 300/2000 en `catalog/registry.py`. Perfil del
      calendario: nunca la base de conocimiento; mover = crear primero y
      cancelar después. **Hueco para T4:** el ejecutor no limita las llamadas
      a las tools ofrecidas por el motor (un run de calendario podría
      ejecutar `search_documents` si el modelo lo inventa). Commit `09cd9fb`.
      RDD: assess `medium` sobre T1-T3 (`slice_budget_reached`, 28 archivos,
      2029 líneas, base `225358a`), consentimiento:
      `declined_this_candidate`.
- [x] T4 — **Router automático**: llamada de clasificación, salto con un solo
      motor, fallback a `knowledge`, auditoría del motor elegido, ambos
      endpoints (`chat` y `chat/stream`), tests con módulo activo
      re-guionados. Ruta: delegada (writer).
      Evidencia: RED colección (`routing` inexistente) + 17 failed → GREEN
      99; writer: ruff ✓, format ✓, 152 enfocados ✓, **suite completa 911
      passed**; spot check parent: 31 ✓ + ruff ✓ + format ✓ + superficies
      exactas (8 archivos, ~834 líneas, ~530 de tests). Nuevo
      `app/modules/agent/routing.py` (`route_message`, 6 turnos de historial
      recortados a 500 caracteres y neutralizados, salida = clave cerrada;
      fallbacks `unrecognized_reply`/`ambiguous_reply`/`tool_call_reply`/
      `llm_error` → `knowledge`). `AgentRun.events()` elige el motor antes de
      auditar; `agent.run_started` suma `engine`, `routed`,
      `router_fallback`, `router_reason`. **Hueco de T3 cerrado:** una tool
      no ofrecida se rechaza antes del ejecutor como `unknown_tool` /
      `tool.not_found` con `reason: not_offered_by_engine`. Cambio de
      auditoría aceptado: una tool oculta por rol o módulo inactivo ya no
      llega al ejecutor (antes `tool.denied` / `tool.inactive_module`); el
      intento sigue auditado y el ejecutor conserva sus controles.
      `AgentRun(engine=None)` ahora significa "rutear". Pendientes (no
      bloquean): solo `LLMError` cae a `knowledge` en el router; otra
      excepción saldría antes de `agent.run_started` sin `agent.run_failed`
      (baja probabilidad); sin temperatura/JSON mode el router no es
      determinista; un modelo chico puede responder la pregunta en vez de
      la clave (cae a `knowledge`); con el provider `fake` offline todo cae
      a `knowledge`; en SSE el primer evento llega después del router.

## Criterios de aceptación

1. Con Calendario activo, "¿qué tengo el día 20?" usa solo `list_events` (sin
   `search_documents`) y el rango corresponde al mes y año actuales en la zona
   del usuario.
2. Una pregunta sobre documentos usa solo `search_documents`, con citas.
3. Sin módulos activos no hay llamada extra al LLM.
4. Router caído o respuesta inválida → `knowledge`, sin 500.
5. `agent.run_started` registra zona, hora local, fallback y motor elegido.
6. Ruff/format limpios; pytest backend verde; web typecheck/lint/test/build
   verdes.

## Checks

- Backend: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/pytest` (enfocados por tarea, suite completa al cierre).
- Web: `npm --prefix web run typecheck`, `npm --prefix web run lint`,
  `npm --prefix web test -- --run`, `npm --prefix web run build`.
- TDD: RED observado antes de implementar en cada tarea con comportamiento.

## Delivery

Estrategia cacheada de la feature anterior: **stacked-to-main**. Rama
`feat/agent-engine-routing` sobre `feat/calendar-mvp` (`225358a`). Pronóstico:
~1.300 líneas (tests incluidos) → slices previstos: PR1 = T1+T2 (contexto
horario), PR2 = T3+T4 (motores y router). Commits work-unit por tarea. RDD:
assess por commit con base en el último límite revisado (punto de rama
`225358a`).

## Progress

- 2026-10-16: doc creado; rama `feat/agent-engine-routing` sobre
  `feat/calendar-mvp`; mapa del agente hecho por un explorer (sin cambios).
- 2026-10-16: T1 (`0ca79f2`) y T2 (`31e57aa`) completas → slice PR1
  (contexto horario) cerrado en commits.
- 2026-10-16: T3 (`09cd9fb`) y T4 completas → slice PR2 (motores y router)
  cerrado en commits. Feature lista para prueba manual.

## Next step

Prueba manual del humano con `make app` (calendario activo, cuenta Google
conectada): "¿qué tengo el día 20?" → auditoría con `engine: calendar` y solo
`list_events`; una pregunta de documentos → `engine: knowledge` con citas.
Push/PR quedan a decisión del humano (slices PR1 = T1+T2, PR2 = T3+T4).
