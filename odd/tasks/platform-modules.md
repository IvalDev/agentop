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

### Definición del producto y superficies (descrito por el humano 2026-10-14)

**Un asistente personal de trabajo para el día a día de cada empleado**, que conoce la empresa (su base de conocimiento) y, con permiso, sus herramientas (correo, calendario, facturas…), y que la propia empresa configura desde una consola web.

| Superficie | Para quién | Contenido |
| --- | --- | --- |
| **Consola web** (navegador del ordenador) | Todos; cada sección según rol | Dashboard con tres secciones: **Chat**, **Base de conocimiento** y **Configuración** (alta de empleados con roles; activación y configuración de las herramientas del agente). Incluye aprobaciones |
| **Widget de escritorio** | Quien solo necesita hablar con el bot | Solo chat, siempre a mano (bandeja, atajo global, ventana flotante) |
| **App móvil** (PWA instalada) | Todos | Solo chat |

| Sección | owner / admin | operator | viewer |
| --- | --- | --- | --- |
| Chat | ✅ | ✅ | ✅ |
| Base de conocimiento | Ver y subir | Ver y subir | Ver |
| Configuración: equipo y roles | ✅ | — | — |
| Configuración: herramientas y módulos | ✅ | — | — |
| Aprobaciones | Según el rol que exige cada herramienta | Según el rol que exige cada herramienta | — |

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
6. **Los datos personales sensibles (nóminas, salarios, datos de salud) no pasan por el modelo**, y menos por un proveedor externo: se procesan con código determinista y el modelo solo ve metadatos (p. ej. "12 nóminas listas para enviar"). Tratamiento sujeto al RGPD.

## Checklist de tareas

### Fase 0 — Decisiones (antes de implementar)

- [x] D1 — **Canal de chat. Decidido por el humano 2026-10-14:** una sola interfaz web de chat (WCAG 2.2 AA: teclado, lector de pantalla, contraste, zoom, movimiento reducido, adaptable a móvil) entregada de tres formas:
  - **Móvil:** PWA de solo chat instalada en la pantalla de inicio (Android e iOS), con notificaciones push web. Si más adelante hace falta estar en las tiendas, se envuelve la misma web (p. ej. Capacitor)
  - **Escritorio, "widget":** aplicación ligera que envuelve la misma web y vive en la bandeja del sistema, con atajo de teclado global que abre una ventana flotante de chat, opción de "siempre visible", arranque con el sistema y notificaciones nativas. Propuesta: Tauri (usa el navegador del sistema, instalador de pocos MB) frente a Electron (más pesado). Alternativa mínima: la PWA instalada en el escritorio, que abre en su propia ventana pero sin atajo global ni bandeja. **Tecnología pendiente de confirmar**
  - **Navegador:** la misma web en una dirección
  - Complementos posteriores: chat embebible en la intranet o sistema de la empresa, y canales de mensajería (WhatsApp Business, Telegram, Teams) como módulos
  - Restricción derivada: PWA y notificaciones exigen HTTPS con certificado válido, y el móvil fuera de la oficina necesita llegar al servidor de la empresa (dirección pública con HTTPS o VPN). Afecta a T12 (deployment) de `admin-agent-core`
- [ ] D2 — **Stack del frontend de producción.** La consola `/ui` (HTML + JS sin build) es solo de desarrollo. Opciones: React + Vite + TypeScript con atomic design y container/presentational (encaja con la experiencia del equipo), u otra. Depende de D1. **Pendiente**
- [ ] D3 — **Conectores de correo y calendario.** Los proveedores varían por empresa: interfaz de conector común con adaptadores (Google Workspace, Microsoft 365 / Graph, IMAP/SMTP, CalDAV). Decidir el orden de los adaptadores. **Pendiente**
- [x] D5 — **Conexiones de empresa y personales. Decidido por el humano 2026-10-14: ambas.** De empresa: p. ej. gestionar el buzón `facturas@` o enviar a cada empleado su nómina. Personales: "gestiona mi buzón", "revisa mi agenda". Consecuencias de diseño:
  - Cada conexión tiene titular: la empresa (la configura un administrador en Configuración) o un usuario (la configura él mismo en "Mis conexiones", sea cual sea su rol)
  - Una conexión de empresa declara qué roles pueden usarla. Una conexión personal solo la usa el agente cuando habla con su titular: ni un administrador puede leer el buzón de otro empleado (como mucho, ver que existe y revocarla)
  - Las herramientas reciben la conexión sobre la que actúan y el backend valida que el usuario puede usarla; el agente solo ve las conexiones permitidas al usuario actual
  - Aprobaciones: una acción sobre una conexión personal (p. ej. enviar desde mi buzón) la aprueba su titular, no un rol. El modelo actual de aprobaciones (A7) resuelve por rol; hace falta aprobador = usuario concreto
  - Automatismos: los de empresa actúan en nombre de la empresa con aprobación por rol; los personales, en nombre del titular y con su aprobación
- [ ] D6 — **Acceso desde fuera de la oficina.** ¿El móvil y el widget se usan fuera de la red de la empresa? Si es así: dirección pública con HTTPS o VPN (afecta a T12). **Pendiente**
- [ ] D4 — **Estrategia de entrega** de la feature (la previsión supera con mucho ~400 líneas): `ask-on-risk`; la estrategia de cadena (`stacked-to-main` o `feature-branch-chain`) se pregunta antes del primer commit de implementación

### Fase 1 — Plataforma

- [ ] P1 — Alta de empresa y administrador: comando de instalación (primer arranque) que crea el tenant y su administrador; sin depender del seed de demo
- [ ] P2 — Gestión de usuarios y roles: API de administración para crear, listar, desactivar y cambiar el rol de usuarios dentro de la empresa, y restablecer contraseñas; auditada; tests de aislamiento entre empresas
- [ ] P3 — Catálogo de módulos y activación por empresa: manifiesto de módulo, estado por empresa (activo, configuración, salud) y API para listar, activar y desactivar. El agente solo ofrece las herramientas de los módulos activos de la empresa, además del filtro por rol actual
- [ ] P4 — Credenciales cifradas por empresa: cifrado en reposo con clave fuera de la base de datos; nunca se devuelven por API ni se envían al modelo; rotación y auditoría de uso
- [ ] P5 — Motor de onboarding: pasos declarados por el módulo (formulario, redirección OAuth, selección de alcance, prueba de conexión), estado por empresa y reanudable; la UI los muestra de forma genérica
- [ ] P6 — Worker y automatismos: reglas "cuando/si/entonces" con disparadores de los módulos (eventos y horarios); las acciones pasan por el ejecutor de herramientas (mismos roles, aprobaciones y auditoría); worker en segundo plano según A1 (Redis + RQ)
- [ ] P8 — Conexiones de empresa y personales (D5): titular de cada conexión, roles permitidos en las de empresa, uso exclusivo del titular en las personales, sección "Mis conexiones" para todos los usuarios y aprobador = titular para acciones sobre conexiones personales. Tests de que ningún usuario, ni un administrador, accede a conexiones personales ajenas
- [ ] P9 — Permisos por documento: hoy cualquier rol recupera cualquier documento de la empresa. Documentos visibles solo para ciertos roles o personas (p. ej. la nómina de un empleado, solo para él y RRHH), aplicado en la búsqueda y en la citación. Requisito previo de M5 y de cualquier dato sensible en la base de conocimiento
- [ ] P7 — Generalizar la política de respuestas de T9.5: hoy la base de conocimiento es la única fuente de hechos; con módulos, la fuente son los resultados de las herramientas, sin perder la prohibición de inventar

### Fase 2 — Canal de chat (tras D1 y D2)

- [ ] C1 — Consola web de producción: login y dashboard con las secciones Chat (con citas), Base de conocimiento, Configuración (equipo y roles; herramientas, módulos y onboarding) y aprobaciones, visibles según el rol. Modo "solo chat" de la misma web, instalable como PWA en el móvil (D1). WCAG 2.2 AA verificada. Resolver al pasar de la consola: estáticos como package data, y no montar UI de desarrollo si falta `AA_ENVIRONMENT`
- [ ] C2 — Widget de escritorio (según D1) para quien solo necesita hablar con el bot: envoltorio ligero del modo "solo chat" de la misma web con bandeja del sistema, atajo global, ventana flotante, arranque con el sistema y notificaciones nativas; instaladores para Windows, macOS y Linux
- [ ] C2.b — Chat embebible en la intranet o sistema de la empresa (posterior)
- [ ] C3 — Canales de mensajería como módulos (WhatsApp Business, Telegram, Teams), con el mismo contrato de módulo

### Fase 3 — Módulos

- [ ] M1 — Facturas (diseño preservado en T11.b/T11.c de `admin-agent-core`): `extract_invoice`, `list_invoices` (pendientes, vencimientos, por proveedor), `register_invoice` con aprobador según importe (PRC-FIN-02), verificación determinista y suite e2e. Primer módulo: valida el contrato de módulo sin depender de servicios externos
- [ ] M2 — Calendario: conectores (según D3); onboarding (conectar cuenta o calendario local, elegir calendarios, permisos de lectura/escritura); herramientas `list_events` y `find_free_slots` (AUTO), y `create_event`, `update_event` y `cancel_event` (REQUIRE_APPROVAL)
- [ ] M3 — Correo, lectura: conectores; onboarding (conectar cuenta, elegir buzón, alcance); herramientas `search_emails`, `read_email` y `draft_reply` (solo borrador); disparador "correo nuevo"; automatismos de clasificación e ingesta de adjuntos en la base de conocimiento
- [ ] M4 — Correo, envío: `send_email` siempre con aprobación, mostrando destinatarios y texto exactos; lista de destinatarios permitidos y límite de envíos
- [ ] M5 — Nóminas (requiere P8, P9 y M4): envío de cada nómina a su empleado desde la conexión de empresa. Herramienta determinista: el emparejamiento nómina → empleado y el envío los hace el código, solo a la dirección corporativa registrada del empleado; el modelo orquesta pero nunca lee el contenido de las nóminas. Aprobación del lote completo por RRHH o administración, mostrando la lista exacta de destinatarios; auditoría por envío

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

D1 decidido (consola web + widget de escritorio y app móvil de solo chat) y D5 decidido (conexiones de empresa y personales). Falta decidir D6 (acceso fuera de la oficina), D2 (stack del frontend), D3 (orden de conectores) y la tecnología del widget (Tauri propuesto), y autorizar la Fase 1 (P1–P3 como primer bloque).
