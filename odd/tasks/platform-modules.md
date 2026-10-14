# Feature: Plataforma de módulos, onboarding y canal de chat (platform-modules)

**Creado:** 2026-10-14 · **Estado:** planificada (sin implementación autorizada)
**Rama prevista:** `feat/platform-modules`, desde `main` cuando se integre `feat/admin-agent-core` (decisión del humano)
**Relación:** se construye sobre `odd/tasks/admin-agent-core.md`. Su T11.b/T11.c (facturas) pasa a M1 de este documento; T12 (deployment) sigue allí.

## Objetivo

Convertir el núcleo del agente en un producto que cada empresa pone en marcha sola. Se instala con un usuario administrador y se crea la empresa (tenant). Desde el primer momento funcionan las capacidades básicas: usuarios con roles, documentos y chat. Después, la empresa activa servicios (correo, calendario, facturas…) y cada uno tiene un onboarding guiado: conectar la cuenta, elegir qué gestiona el bot, conceder permisos y definir automatismos.

## Problema / Por qué

Hoy la empresa y sus usuarios solo se crean con el seed de demo, no existe API de gestión de usuarios, el registro de herramientas es global (se filtra por rol, no por servicios contratados) y la única UI es una consola de desarrollo. Las empresas usan proveedores distintos de correo y calendario, así que cada servicio necesita conectores intercambiables y una configuración guiada. El chat tiene que llegar a la gente donde está, en el navegador y en el móvil, y ser lo más accesible posible.

## Visión

| Capa | Contenido |
| --- | --- |
| **Núcleo** (siempre activo) | Alta de empresa y administrador, usuarios y roles, documentos y RAG, chat, aprobaciones, auditoría |
| **Módulos** (activables por empresa) | Facturas, calendario, correo, canales de mensajería… Cada uno cumple el contrato de módulo |
| **Automatismos** | Reglas "cuando X, si Y, hacer Z" que corren sin chat; las acciones sensibles pasan por aprobación |
| **Canales de chat** | Web/PWA (principal), widget embebible y canales de mensajería como módulos |

### Contrato de módulo

| Pieza | Descripción | Ejemplo (correo) |
| --- | --- | --- |
| Manifiesto | Nombre, descripción, permisos que solicita, versión | "Leer y redactar correos de un buzón" |
| Conectores | Adaptadores intercambiables por proveedor | Google, Microsoft 365, IMAP/SMTP |
| Credenciales | Cifradas por empresa; nunca se devuelven por API ni llegan al modelo | Token OAuth o usuario/contraseña IMAP |
| Onboarding | Pasos declarados por el módulo y renderizados por la UI de forma genérica | Conectar cuenta → elegir buzón → permisos → probar conexión |
| Alcance | Qué puede tocar; el backend lo aplica (precheck), no el prompt | Solo `facturas@`, solo lectura |
| Herramientas | Lo que aporta al agente, con rol mínimo y política de aprobación | `search_emails` (AUTO), `send_email` (REQUIRE_APPROVAL) |
| Disparadores | Eventos para automatismos | "Llegó un correo nuevo" |
| Estado | Salud de la conexión, visible para el administrador | "El token venció: vuelve a conectar" |

## Principios de seguridad (restricciones de diseño)

1. **Las credenciales nunca pasan por el chat ni por el modelo.** El chat puede viajar a un proveedor externo (Groq en desarrollo). Conectar una cuenta se hace con un formulario o una redirección OAuth que va directo al backend. El bot puede guiar el proceso y ayudar a redactar reglas en lenguaje natural, que el usuario confirma.
2. **El contenido de correo, calendario y adjuntos es dato no confiable**: va delimitado y nunca dispara herramientas por sí mismo (regla 2 del prompt de sistema).
3. **La comunicación hacia fuera siempre requiere aprobación humana**, mostrando destinatarios y texto exactos, con lista de destinatarios permitidos y límite de envíos. Un agente que lee correo y puede enviarlo reúne datos privados, contenido de terceros y salida al exterior: es el escenario clásico de exfiltración.
4. **Cada automatismo declara qué hace sin humano** (p. ej. clasificar o etiquetar) y qué requiere aprobación (responder, enviar, agendar).
5. **El alcance y los permisos se aplican en el backend**, no en el prompt. Todo queda en la auditoría.

## Checklist de tareas

### Fase 0 — Decisiones (antes de implementar)

- [ ] D1 — **Canal de chat.** Recomendación: aplicación web progresiva (PWA) como canal principal: una sola base de código, se abre con una dirección en el navegador y se instala en el móvil y en el escritorio, sin tiendas de aplicaciones. Accesibilidad WCAG 2.2 AA como requisito (teclado, lector de pantalla, contraste, zoom, movimiento reducido, diseño adaptable a móvil). Complementos: widget embebible para integrar el chat en la intranet o el sistema de la empresa ("nativo en el sistema"), y canales de mensajería (WhatsApp Business, Telegram, Teams) como módulos más adelante. Aplicación nativa solo si la PWA se queda corta (p. ej. notificaciones o integración con el sistema operativo). **Pendiente de decisión del humano**
- [ ] D2 — **Stack del frontend de producción.** La consola `/ui` (HTML + JS sin build) es solo de desarrollo. Opciones: React + Vite + TypeScript con atomic design y container/presentational (encaja con la experiencia del equipo), u otra. Depende de D1. **Pendiente**
- [ ] D3 — **Conectores de correo y calendario.** Los proveedores varían por empresa: interfaz de conector común con adaptadores (Google Workspace, Microsoft 365 / Graph, IMAP/SMTP, CalDAV). Decidir el orden de los adaptadores. **Pendiente**
- [ ] D4 — **Estrategia de entrega** de la feature (la previsión supera con mucho ~400 líneas): `ask-on-risk`; la estrategia de cadena (`stacked-to-main` o `feature-branch-chain`) se pregunta antes del primer commit de implementación

### Fase 1 — Plataforma

- [ ] P1 — Alta de empresa y administrador: comando de instalación (primer arranque) que crea el tenant y su administrador; sin depender del seed de demo
- [ ] P2 — Gestión de usuarios y roles: API de administración para crear, listar, desactivar y cambiar el rol de usuarios dentro de la empresa, y restablecer contraseñas; auditada; tests de aislamiento entre empresas
- [ ] P3 — Catálogo de módulos y activación por empresa: manifiesto de módulo, estado por empresa (activo, configuración, salud) y API para listar, activar y desactivar. El agente solo ofrece las herramientas de los módulos activos de la empresa, además del filtro por rol actual
- [ ] P4 — Credenciales cifradas por empresa: cifrado en reposo con clave fuera de la base de datos; nunca se devuelven por API ni se envían al modelo; rotación y auditoría de uso
- [ ] P5 — Motor de onboarding: pasos declarados por el módulo (formulario, redirección OAuth, selección de alcance, prueba de conexión), estado por empresa y reanudable; la UI los muestra de forma genérica
- [ ] P6 — Worker y automatismos: reglas "cuando/si/entonces" con disparadores de los módulos (eventos y horarios); las acciones pasan por el ejecutor de herramientas (mismos roles, aprobaciones y auditoría); worker en segundo plano según A1 (Redis + RQ)
- [ ] P7 — Generalizar la política de respuestas de T9.5: hoy la base de conocimiento es la única fuente de hechos; con módulos, la fuente son los resultados de las herramientas, sin perder la prohibición de inventar

### Fase 2 — Canal de chat (tras D1 y D2)

- [ ] C1 — Aplicación de producción: login, chat con citas, documentos, panel de aprobaciones y administración (usuarios, módulos, onboarding). Adaptable a móvil, instalable si D1 confirma PWA, WCAG 2.2 AA verificada. Resolver al pasar de la consola: estáticos como package data, y no montar UI de desarrollo si falta `AA_ENVIRONMENT`
- [ ] C2 — Widget de chat embebible en sistemas de la empresa (si D1 lo confirma)
- [ ] C3 — Canales de mensajería como módulos (WhatsApp Business, Telegram, Teams), con el mismo contrato de módulo

### Fase 3 — Módulos

- [ ] M1 — Facturas (diseño preservado en T11.b/T11.c de `admin-agent-core`): `extract_invoice`, `list_invoices` (pendientes, vencimientos, por proveedor), `register_invoice` con aprobador según importe (PRC-FIN-02), verificación determinista y suite e2e. Primer módulo: valida el contrato de módulo sin depender de servicios externos
- [ ] M2 — Calendario: conectores (según D3); onboarding (conectar cuenta o calendario local, elegir calendarios, permisos de lectura/escritura); herramientas `list_events` y `find_free_slots` (AUTO), y `create_event`, `update_event` y `cancel_event` (REQUIRE_APPROVAL)
- [ ] M3 — Correo, lectura: conectores; onboarding (conectar cuenta, elegir buzón, alcance); herramientas `search_emails`, `read_email` y `draft_reply` (solo borrador); disparador "correo nuevo"; automatismos de clasificación e ingesta de adjuntos en la base de conocimiento
- [ ] M4 — Correo, envío: `send_email` siempre con aprobación, mostrando destinatarios y texto exactos; lista de destinatarios permitidos y límite de envíos

## Authorized scope

Solo planificación (pedido del humano 2026-10-14: "añádelo a las tareas"). Ninguna tarea de implementación está autorizada todavía; cada fase se autoriza por separado.

## Acceptance criteria

1. Una instalación nueva crea su empresa y su administrador sin el seed de demo, y el administrador gestiona usuarios y roles por API y UI.
2. Una empresa activa y desactiva módulos; el agente solo ofrece herramientas de los módulos activos y del rol del usuario.
3. Las credenciales se guardan cifradas, no aparecen en respuestas de la API, en logs, en la auditoría ni en mensajes al modelo (verificado por tests).
4. Cada módulo completa su onboarding desde la UI, reanudable, con prueba de conexión.
5. Los automatismos se ejecutan sin chat, respetan roles y aprobaciones y quedan auditados.
6. El chat de producción funciona en navegador de escritorio y móvil y cumple WCAG 2.2 AA en sus flujos principales.
7. Ningún envío hacia fuera ocurre sin aprobación humana explícita.

## Checks por tarea

- `ruff check . && ruff format --check .` y `pytest -q` en cada work unit
- Conectores probados con dobles (fakes) en tests; nunca cuentas reales en la suite
- Tests de aislamiento entre empresas y de no-fuga de credenciales en cada tarea que toque datos o secretos
- Frontend: tests de componentes y comprobación automática de accesibilidad (p. ej. axe) más una revisión manual con teclado y lector de pantalla
- Recorrido e2e por módulo documentado en `docs/`

## Progress / evidence

- 2026-10-14: documento creado a partir de la conversación de producto; nada implementado. Previsión: varios miles de líneas en total, así que se aplicará `ask-on-risk` (D4)

## Next step

Decidir D1 (canal de chat) y D3 (orden de conectores); después, autorizar la Fase 1 (P1–P3 como primer bloque).
