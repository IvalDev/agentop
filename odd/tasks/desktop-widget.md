# Feature: Widget de escritorio (desktop-widget)

**Creado:** 2026-10-17 · **Estado:** aprobado por el humano (2026-10-17) — implementación en curso: T0 completada y verificada; T1 siguiente
**Rama prevista:** `feat/desktop-widget`, apilada sobre `feat/agent-engine-routing` (extremo actual de la pila de trabajo local)
**Relación:** adelanta C2 de `odd/tasks/platform-modules.md` (decisiones D1, D2, D4, D7, D8). El chat web que envuelve ya existe y funciona (Fase 0.5, tarea L3).

## Objetivo

Hablar con el asistente sin abrir el navegador. Una aplicación de escritorio ligera (Tauri 2 + el motor web del sistema) que envuelve el mismo chat de la web en modo "solo chat" y vive en la bandeja del sistema: atajo que abre una ventana flotante, opción de "siempre visible", arranque con el sistema y notificaciones nativas. Linux primero (la máquina del humano); Windows y macOS después.

Y que llegar ahí sea fácil: la consola web ofrece un botón **«Instalar widget»** que detecta el sistema del usuario y entrega el instalador correcto con instrucciones paso a paso (pedido del humano 2026-10-17).

## Por qué ahora

- Pedido del humano 2026-10-17: tener el widget en su equipo (Ubuntu) para chatear sin navegar la consola web.
- D1 lo dejó planificado con la tecnología pendiente; el humano confirmó **Tauri** el 2026-10-17.
- C1 (consola de producción) no es requisito: el chat web ya existe y el widget solo necesita un modo solo-chat y el envoltorio.

## Decisión de tecnología (W1 — confirmada por el humano 2026-10-17)

**Tauri 2** por sobre Electron: usa el motor web del sistema (WebKitGTK en Linux, WebView2 en Windows, WKWebView en macOS), instalador de pocos MB y sin empaquetar un navegador completo.

Consecuencias asumidas:
- Requiere toolchain Rust y librerías de sistema en Linux (hoy **no están** en la máquina del humano; T0 las prepara).
- El widget no reimplementa UI: carga la URL del servidor de la empresa (el mismo SPA servido por FastAPI). Sin servidor alcanzable no hay widget: es un cliente.

## Entorno verificado (2026-10-17, máquina del humano)

- Ubuntu 26.04, GNOME 50.1, sesión **Wayland**.
- Bandeja: extensión `ubuntu-appindicators@ubuntu.com` activa → los iconos de bandeja se muestran en GNOME.
- `libwebkit2gtk-4.1-dev` 2.52.6, `libayatana-appindicator3-dev` 0.5.94, `libsoup-3.0` 3.6.6, `librsvg2-dev`, `libxdo-dev`, `build-essential` y `libssl-dev` **instalados** (sudo del humano, 2026-10-17).
- Rust/cargo 1.99.1 + clippy/rustfmt instalados vía rustup (2026-10-17).
- `notify-send` presente (notificaciones nativas funcionan).
- Atajos globales: el portal `org.freedesktop.portal.GlobalShortcuts` existe en GNOME 50, pero el plugin `global-shortcut` de Tauri registra vía X11 → **en Wayland no funcionará nativo**. W4 define el mecanismo.

## Hechos de integración (mapa 2026-10-17)

- **Servido:** `make app` compila `web/` y FastAPI sirve el SPA en el puerto 8000 (`Makefile:68-73`; `app/main.py:151-160,231`); en dev, Vite en 5173 con proxy `/api` → 8000 (`web/vite.config.ts:9-12`).
- **Mismo origen:** el frontend solo llama a rutas `/api/` del mismo origen, sin URL base (`web/src/api/client.ts:61-66`). Cargar la URL del servidor en la ventana del widget da SPA y API en un solo origen, sin cambios de CORS.
- **Sesión (bloqueante para la UX del widget):** token en `sessionStorage`, expira a los **30 minutos**, sin refresh ni cookies (`web/src/session/tokenStorage.ts:2-4`; `app/core/config.py:54`; el router de identidad no tiene refresh). En el widget eso significa login cada 30 min y en cada reinicio → lo resuelve T2.
- **Rutas:** todo vive bajo `RequireAuth` + `DashboardShell`; `ChatProvider` se monta en el shell (`web/src/pages/DashboardShell.tsx:41-43`); `ChatPage` solo necesita `SessionProvider` + `ChatProvider` (`web/src/pages/ChatPage.tsx:14`) → el modo solo-chat es una ruta nueva con un shell mínimo, no un rediseño.
- **Chat:** SSE por `fetch` + `ReadableStream` (no `EventSource`), eventos `run_started, token, tool_call, tool_result, approval_required, citations, done, error` (`web/src/api/chat.ts:58-100,141-169`).
- **No existe:** PWA/manifest/service worker, `desktop/`, código Tauri, job de web en CI (`web/index.html`; `.github/workflows/ci.yml` — solo ruff+pytest).
- **Repo:** no hay rama `main` ni remoto configurado (verificado 2026-10-17); la pila local es `feat/admin-agent-core → feat/local-app → feat/calendar-mvp → feat/agent-engine-routing`.

## Diseño propuesto

- **W2 — Modo solo-chat:** ruta `/widget` en `web/`, fuera del `DashboardShell`: shell mínimo (título + cerrar sesión) que monta `ChatProvider` + `ChatPage`; sin navegación de consola; login embebido cuando no hay sesión; accesibilidad base conservada (foco, títulos, axe).
- **W3 — Sesión persistente para clientes instalados:** refresh token en cookie httpOnly (SameSite=Strict, `Path=/api/v1/auth`, `Secure` según entorno) + endpoints `refresh` y `logout` en el backend, rotación en cada uso y revocación en el servidor (tabla con hash, migración). El access token sigue en memoria; al abrir o recargar, la sesión se restaura sola vía cookie. Adelanta parte de P2 (como L1 adelantó la API de equipo); es la base de las "sesiones por dispositivo" de D6.
- **W4 — Atajo global:** en Wayland, comando `admin-agent-widget --toggle` + **atajo personalizado de GNOME** documentado (Ajustes → Teclado → Atajos personalizados); la instancia única recibe el toggle por IPC. En X11, además, registro nativo con el plugin. T0 valida el mecanismo en la máquina.
- **W5 — Notificaciones nativas:** al terminar una respuesta con la ventana oculta o sin foco; clic en la notificación muestra y enfoca la ventana. El puente web↔nativo no agrega dependencias de Tauri al SPA.
- **W6 — Servidor configurable:** variable `AA_SERVER_URL` → archivo XDG de configuración → default `http://127.0.0.1:8000`. UI de ajustes diferida (es parte de C1/producción); el archivo queda documentado.
- **W7 — Instalación facilitada desde la consola (pedido del humano 2026-10-17):** entrada "Widget de escritorio" visible para todos los roles, con el botón **«Instalar widget»**: detecta el SO de forma best-effort (familia por user agent / Client Hints; en Linux el usuario elige `.deb` —Ubuntu/Debian— o AppImage; arquitectura x86_64 por defecto) y ofrece el instalador servido por el backend (Windows/macOS: "próximamente" hasta F1), con instrucciones paso a paso en es-ES (descargar → abrir → Instalar; la tienda de Ubuntu resuelve sola las dependencias del `.deb`). **Límite honesto:** una página web no puede instalar en silencio en la máquina; haría falta un agente local privilegiado y no se justifica. El flujo es descarga + instalación guiada, en dos clics. El arranque con el sistema se activa por defecto en el primer arranque del widget (T4). Auto-login/pairing y el deep link `aa-widget://` («Abrir widget») quedan diferidos (F4).
- **Fuera de alcance (diferido):** instaladores Windows/macOS y su verificación (F1: requieren runners CI o máquinas; no hay remoto todavía), firma y auto-update (F2), PWA móvil (F3, territorio C1), UI de ajustes, nombre/marca definitivos (provisional: "Asistente").

## Checklist de tareas

- [x] T0 — **Toolchain y spike de viabilidad (GNOME Wayland):** instalar Rust (rustup, sin sudo) y los paquetes apt (requieren sudo del humano: `libwebkit2gtk-4.1-dev`, `build-essential`, `libayatana-appindicator3-dev`, `librsvg2-dev`, `libxdo-dev`, `libssl-dev`, `file`, `wget`); scaffold mínimo de Tauri en `desktop/`; verificar en la máquina: ventana que carga `http://127.0.0.1:8000`, icono de bandeja visible, notificación nativa desde la app, y el mecanismo W4 (`--toggle` + atajo de GNOME). Evidencia (2026-10-17): Rust 1.99.1 + clippy/rustfmt; scaffold Tauri 2.12.4 en `desktop/` (single-instance 2.5.4, notification 2.7.0); build debug OK en 1m28s; la ventana cargó el SPA (`GET /` + assets en el log del servidor); bandeja, notificación de prueba y `--toggle` ocultar/mostrar confirmados por el humano en GNOME Wayland; warning de deprecación de libayatana-appindicator: inocuo; mecanismo W4 validado en la máquina (el atajo de GNOME se documenta en T6). Commit `e93981d`. Ruta: inline (bash/scaffold) + explorer para el mapa.
- [ ] T1 — **Modo solo-chat `/widget` en la web:** ruta nueva con shell mínimo (título, cerrar sesión; sin enlace a la consola — pedido del humano: solo el chat), login cuando no hay sesión, reutiliza `ChatPage`/`ChatProvider`; textos en el catálogo i18n; sin cambios de comportamiento en la consola. Ruta: delegated direct (writer). Checks: vitest (ruta, gating de sesión, axe), typecheck, lint
- [ ] T2 — **Sesión persistente:** refresh token en cookie httpOnly con rotación y revocación (tabla con hash + migración), endpoints `POST /api/v1/auth/refresh` y `POST /api/v1/auth/logout`, config de cookie por entorno; en la web: restauración silenciosa al abrir, 401 → un refresh → si falla, sesión expirada, logout llama al servidor; el access token deja de persistir en `sessionStorage`. Tests: rotación, reuso, expiración, revocación, aislamiento entre empresas; web: refresh silencioso y reintento único. Ruta: delegated direct (writer). ⚠️ Candidato de riesgo alto/medio: consentimiento RDD por candidato
- [ ] T3 — **Shell Tauri:** proyecto en `desktop/` (Tauri 2), ventana principal que carga `{AA_SERVER_URL}/widget` (W6: env → archivo XDG → default localhost:8000), navegación restringida al origen configurado (enlaces externos al navegador del sistema), instancia única, cerrar oculta (sin bandeja todavía: hide llega con T4; hasta entonces cerrar puede salir), targets `make widget-dev` / `make widget-build`. Ruta: delegated direct (writer). Checks: `cargo check` + `cargo clippy` + smoke manual
- [ ] T4 — **Experiencia de shell:** bandeja con menú (Mostrar/Ocultar, "Siempre visible" ✓, "Arranque con el sistema" ✓, Salir), cerrar → bandeja, `--toggle` con IPC sobre la instancia única, atajo nativo en X11 + documentación del atajo de GNOME para Wayland (W4), autostart (plugin; propuesta: activado por defecto en el primer arranque). Ruta: delegated direct (writer). Checks: `cargo clippy` + smoke manual documentado
- [ ] T5 — **Notificaciones nativas (W5):** notificación al completar la respuesta con ventana oculta/sin foco (evento `done` del stream), clic enfoca la ventana; puente web↔nativo sin dependencias Tauri en el SPA. Ruta: delegated direct (writer). Checks: tests web del disparador + smoke manual
- [ ] T6 — **Empaquetado Linux + docs:** `.deb` + AppImage (`tauri build --bundles`), iconos, `make widget-package`, `docs/desktop-widget.md` (instalación, atajo de GNOME, autostart, troubleshooting de bandeja, protocolo de prueba manual). Ruta: delegated direct (writer). Checks: instalación real del `.deb` en la máquina + protocolo manual completo
- [ ] T7 — **Botón «Instalar widget» + publicación de instaladores:** entrada visible para todos los roles en la consola web; detección de SO y oferta del instalador (Linux: `.deb`/AppImage; Windows/macOS: «próximamente»); los artefactos de T6 se publican servidos por el backend (ruta estática, sin secretos, versión en el nombre); instrucciones por SO en el catálogo i18n; tests web (detección, render, axe) y backend (ruta de descarga). Depende de T6 (puede probarse con un artefacto de prueba). Ruta: delegated direct (writer)

Diferidas (fuera del MVP): F1 Windows/macOS (empaquetado + verificación; requiere runners CI o máquinas), F2 firma/auto-update, F3 PWA móvil, F4 auto-login/pairing del widget y deep link `aa-widget://` («Abrir widget» desde la consola).

## Riesgos conocidos

- **Atajos globales en Wayland:** el plugin de Tauri no los soporta; mitigado con W4 (toggle por comando + atajo de GNOME). T0 lo valida y registra evidencia.
- **Bandeja en GNOME:** los appindicator no tienen "clic izquierdo → abrir" uniforme; el menú es la interfaz principal (Mostrar/Ocultar). Documentado en T6.
- **T2 toca autenticación:** cookie/CSRF/rotación; tests dedicados y consentimiento RDD por candidato.
- **Sin remoto ni `main`:** no hay PRs posibles hoy; la entrega es local en la rama (ver Entrega).
- **El widget depende de un servidor alcanzable:** en esta máquina, localhost; fuera de la oficina aplica D6/D7 (diferido).

## Criterios de aceptación (MVP Linux)

1. `make widget-dev` abre una ventana con el modo solo-chat contra el servidor local; el chat funciona igual que en el navegador (streaming, citas, aprobaciones).
2. La ventana vive en la bandeja (icono visible en GNOME); cerrarla la oculta; el menú abre/oculta y permite salir.
3. `--toggle` muestra/oculta desde cualquier aplicación (verificado con el atajo personalizado de GNOME en Wayland; nativo en X11 si T0 lo confirma).
4. Los toggles de "siempre visible" y "arranque con el sistema" funcionan y persisten.
5. Una respuesta con la ventana oculta produce una notificación nativa; el clic enfoca la ventana.
6. La sesión sobrevive al reinicio del widget (T2) sin volver a escribir la contraseña, hasta que el refresh expire o el servidor lo revoque.
7. `make widget-package` produce un `.deb` instalable en Ubuntu 26.04 y un AppImage ejecutable.
8. Sin regresión de accesibilidad en el modo solo-chat (axe en tests + revisión manual con teclado).
9. El widget no guarda credenciales ni datos propios: todo vive en el WebView contra el servidor.
10. La consola muestra el botón «Instalar widget» a todos los roles: detecta el SO, entrega el instalador correcto con instrucciones paso a paso y los artefactos publicados son los del último release.

## Checks

- Web: `make web-test` (typecheck + lint + vitest, incluye axe) en cada tarea que lo toque.
- Backend (T2): `ruff check . && ruff format --check . && pytest -q`.
- Rust (T3–T6): `cargo check` + `cargo clippy` en `desktop/src-tauri`; tests unitarios donde aporten (config de URL, parseo de argumentos); smoke manual por el protocolo de T6.
- RDD por candidato según el interruptor; consentimiento del humano por candidato (T2 es el candidato de riesgo alto/medio).
- El protocolo de prueba manual vive en `docs/desktop-widget.md` (se ejecuta al cerrar T6 y en cada release).

## Entrega

- Estrategia registrada (D4): `ask-on-risk` con cadena `stacked-to-main`, PRs ≤400 líneas revisables — **no ejecutable hoy**: no hay rama `main` ni remoto en este repo (verificado 2026-10-17). Decisión humana pendiente (crear remoto/`main` o seguir local). Hasta entonces: commits work-unit en `feat/desktop-widget`, sin push.
- Previsión: ~2.300–2.900 líneas autorizadas en total (incluye tests y docs). Al superar ~400 acumuladas se aplica la estrategia de entrega elegida; las fronteras de los slices se registran acá.
- Sin `Co-Authored-By` ni atribución de IA en los commits (regla del proyecto).

## Progress / evidence

- 2026-10-17: plan creado tras confirmar Tauri; mapa de integración y entorno verificados (secciones de arriba); nada implementado. Tareas T0–T6 definidas; F1–F3 diferidas.
- 2026-10-17 (2): pedido del humano — botón «Instalar widget» en la consola que instala lo necesario en la máquina del usuario; añadido como W7/T7 (depende de T6); criterio de aceptación 10; F4 registrada.
- 2026-10-17 (3): aprobación del humano; T0 completada y verificada en la máquina (ventana + bandeja + notificación + `--toggle` confirmados visualmente). Commit `e93981d` `feat(desktop): scaffold the Tauri widget shell with tray, toggle and notifications`; entorno actualizado en la sección correspondiente.

## Next step

T1 — Modo solo-chat `/widget` (ruta nueva con shell mínimo, reutiliza `ChatPage`/`ChatProvider`; delegated writer; checks: vitest + typecheck + lint). Después T2 — sesión persistente (candidato RDD de riesgo alto/medio).
