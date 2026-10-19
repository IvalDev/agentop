# Widget de escritorio «Asistente»: instalación y uso en Linux

El widget es una ventana liviana (Tauri 2) que envuelve el mismo chat que la consola web en
`/widget` y vive en la bandeja del sistema. Este documento explica cómo generar los
paquetes, instalarlos, configurar el atajo en GNOME, resolver problemas y ejecutar el
protocolo de prueba manual que cierra cada release.

## Ruta rápida

1. Genera los paquetes: `make widget-package` (produce `.deb` + AppImage en
   `desktop/src-tauri/target/release/bundle/`).
2. Instala el `.deb` (`sudo apt install ./Asistente_0.1.0_amd64.deb`) o marca el AppImage
   con `chmod +x` y ejecútalo (ver [Instalación](#instalación)).
3. Arranca el widget, inicia sesión una vez y comprueba: ventana + bandeja + atajo de
   GNOME + notificación con la ventana oculta (ver
   [Protocolo de prueba manual](#protocolo-de-prueba-manual)).

## Prerrequisitos

| Requisito | Detalle |
|-----------|---------|
| Servidor alcanzable | El widget es un cliente: carga `{servidor}/widget`. En desarrollo, `make app` sirve la consola en <http://127.0.0.1:8000>. Sin servidor no hay chat. |
| Toolchain (solo para generar paquetes) | Rust vía rustup, `libwebkit2gtk-4.1-dev`, `libayatana-appindicator3-dev`, `build-essential`, `libssl-dev`, `librsvg2-dev`, `libxdo-dev` (instalados en T0 con sudo) y Node 22.22+ con npm. |
| Sistema verificado | Ubuntu 26.04, GNOME 50.1, sesión Wayland (X11 también funciona; ver [Atajos](#atajos-de-teclado)). |

## Instalación

Los paquetes se generan desde la raíz del repo:

```bash
make widget-package
```

Artefactos resultantes:

| Artefacto | Ruta |
|-----------|------|
| `.deb` | `desktop/src-tauri/target/release/bundle/deb/Asistente_0.1.0_amd64.deb` (~5,9 MiB) |
| AppImage | `desktop/src-tauri/target/release/bundle/appimage/Asistente_0.1.0_amd64.AppImage` (~87 MiB) |

### Opción A — `.deb` (Ubuntu/Debian)

Doble clic sobre el archivo desde el gestor de archivos (abre la tienda de Ubuntu, que
resuelve las dependencias), o en terminal —**la instalación real la ejecuta el usuario**:

```bash
# recomendado: resuelve dependencias (ayatana-appindicator, webkit2gtk, gtk3)
sudo apt install ./desktop/src-tauri/target/release/bundle/deb/Asistente_0.1.0_amd64.deb

# alternativa clásica
sudo dpkg -i ./desktop/src-tauri/target/release/bundle/deb/Asistente_0.1.0_amd64.deb
sudo apt-get install -f   # solo si dpkg queja por dependencias
```

Instala el binario en `/usr/bin/admin-agent-widget`, el lanzador en el menú de
aplicaciones («Asistente») y los iconos en `usr/share/icons/hicolor/`.

### Opción B — AppImage (sin instalación)

```bash
chmod +x ./desktop/src-tauri/target/release/bundle/appimage/Asistente_0.1.0_amd64.AppImage
./desktop/src-tauri/target/release/bundle/appimage/Asistente_0.1.0_amd64.AppImage
```

Si tu sistema no tiene FUSE, añade `--appimage-extract-and-run` al final del comando.
El AppImage no se registra en el menú de aplicaciones ni en el arranque automático: para
eso usa el `.deb`.

## Primer arranque

- **Bandeja:** aparece el icono «Asistente» y el menú (Mostrar / Ocultar, «Siempre
  visible», «Arranque con el sistema», Salir) es la interfaz principal. En GNOME los
  appindicators no garantizan un «clic izquierdo → abrir» uniforme: usa el ítem
  «Mostrar / Ocultar» del menú (el clic izquierdo alterna cuando el entorno lo permite).
- **Arranque automático:** activado por defecto en el primer arranque; crea
  `~/.config/autostart/Asistente.desktop`. En desarrollo apunta al binario de debug; la
  build instalada apunta a `/usr/bin/admin-agent-widget`. El checkbox del menú de bandeja
  lo activa/desactiva y reescribe el archivo.
- **Servidor (orden de resolución W6):**

  | Orden | Fuente | Detalle |
  |-------|--------|---------|
  | 1 | Variable de entorno `AA_SERVER_URL` | gana siempre |
  | 2 | `$XDG_CONFIG_HOME/admin-agent-widget/config.toml`, clave `server_url` | sin `XDG_CONFIG_HOME` se usa `~/.config` |
  | 3 | Por defecto | `http://127.0.0.1:8000` |

  Archivo de ejemplo (`~/.config/admin-agent-widget/config.toml`):

  ```toml
  server_url = "http://192.168.1.10:8000"
  ```

  Un archivo ausente o malformado no rompe nada: se usa el valor por defecto y el archivo
  no se toca.
- **Sesión:** la cookie de refresh (30 días) restaura el login al abrir o reiniciar el
  widget; no hace falta volver a escribir la contraseña hasta que expire o el servidor la
  revoque.

## Atajos de teclado

| Sesión | Atajo | Mecanismo |
|--------|-------|-----------|
| GNOME Wayland | atajo personalizado de GNOME | `admin-agent-widget --toggle` |
| X11 | `Ctrl+Alt+W` | registro nativo del propio widget |

En Wayland (la sesión de esta máquina) el plugin de atajos globales de Tauri registra vía
X11 y no se inicializa, así que el atajo se configura una vez en GNOME:

1. Ajustes → Teclado → Ver y personalizar atajos → Atajos personalizados → **+**.
2. Nombre: `Asistente` (o el que prefieras).
3. Comando: `admin-agent-widget --toggle` (el binario instalado está en el `PATH`; en
   desarrollo usa la ruta completa `.../desktop/src-tauri/target/debug/admin-agent-widget --toggle`).
4. Asigna la combinación (p. ej. `Ctrl+Alt+A`; evita `Ctrl+Alt+W` si ya lo usa X11).

La instancia única recibe el toggle por IPC: si el widget está corriendo, muestra/oculta;
si no, lo arranca.

## Uso diario

| Acción | Comportamiento |
|--------|----------------|
| Cerrar la ventana (X) | **Oculta**, no sale. El widget sigue en la bandeja; solo «Salir» termina el proceso. |
| Siempre visible | Checkbox del menú de bandeja; persiste entre reinicios. |
| Arranque con el sistema | Activado por defecto; persiste entre reinicios. |
| Notificación | Solo cuando la respuesta termina con la ventana **oculta, minimizada o sin foco**. El clic en la notificación muestra y enfoca la ventana. Con la ventana visible y enfocada no se notifica (por diseño). |

> **Caveat — servidor no por defecto y notificaciones:**
> la capability de Tauri (`desktop/src-tauri/capabilities/widget-bridge.json`) solo
> permite el puente de notificación desde `http://127.0.0.1:8000` (`remote.urls`). Si
> usas un `AA_SERVER_URL` o `server_url` distinto, el comando es rechazado y las
> notificaciones **dejan de llegar en silencio** hasta añadir esa URL a `remote.urls`.
> La ACL se resuelve en tiempo de compilación: tocar la capability exige **recompilar**
> (`make widget-build` / `make widget-package`) y reinstalar.

## Solución de problemas

| Síntoma | Causa y solución |
|---------|------------------|
| No aparece el icono de bandeja en GNOME | Falta la extensión de appindicators: instala/activa `ubuntu-appindicators` (Ubuntu: «Extensión de soporte para indicadores de aplicaciones y KStatusNotifier») con GNOME Extensions o Snap Store. |
| Mensajes `Gtk-CRITICAL` (scale-factor) o aviso `libayatana-appindicator is deprecated` al arrancar | Ruido cosmético conocido de WebKitGTK/appindicator. Es inofensivo; la ventana y la bandeja funcionan. No requiere acción. |
| No llega la notificación | 1) La ventana estaba visible y enfocada (comportamiento esperado). 2) El servidor no es `http://127.0.0.1:8000` → ver el caveat de `remote.urls` arriba. 3) Notificaciones del sistema desactivadas en GNOME. |
| Pide la contraseña al abrir | La sesión expiró (refresh de 30 días o revocación en el servidor). Vuelve a iniciar sesión; la cookie vuelve a restaurarla. |
| El arranque automático lanza una versión vieja o de desarrollo | `~/.config/autostart/Asistente.desktop` apunta al binario equivocado. Desactiva «Arranque con el sistema» en el menú de bandeja y actívalo de nuevo: se reescribe apuntando al binario en uso. |
| El atajo de GNOME no responde | Comprueba que el comando del atajo es exactamente `admin-agent-widget --toggle` y que el widget está instalado; el ítem «Mostrar / Ocultar» del menú de bandeja siempre alterna como alternativa. |

## Protocolo de prueba manual

Ejecutar tras instalar el `.deb`, con el servidor en marcha (`make app`). Marcar cada
paso al pasar.

- [ ] **Ventana:** arranca «Asistente» desde el menú de aplicaciones; se abre la ventana
      (440×700) y carga `/widget` (login o sesión ya restaurada).
- [ ] **Bandeja:** el icono «Asistente» está visible; el menú muestra Mostrar / Ocultar,
      «Siempre visible», «Arranque con el sistema» y Salir.
- [ ] **Cerrar oculta:** cerrar la ventana la oculta, el proceso sigue vivo; «Mostrar /
      Ocultar» en el menú de bandeja la vuelve a mostrar.
- [ ] **Toggle por atajo:** con el widget oculto, el atajo de GNOME (`--toggle`) lo
      muestra; de nuevo, lo oculta. (En X11, además `Ctrl+Alt+W`.)
- [ ] **Siempre visible:** activarlo deja la ventana por encima de otras; reiniciando el
      widget sigue activo.
- [ ] **Arranque con el sistema:** tras activarlo, `~/.config/autostart/Asistente.desktop`
      apunta a `/usr/bin/admin-agent-widget`; tras desactivarlo y reactivarlo se
      reescribe igual.
- [ ] **Notificación + clic:** oculta la ventana y envía un mensaje de chat; al terminar
      la respuesta aparece la notificación nativa; el clic muestra y enfoca la ventana.
- [ ] **Negativa:** con la ventana visible y enfocada, envía otro mensaje; **no** debe
      aparecer notificación.
- [ ] **Restauración de sesión:** «Salir» desde la bandeja y relanzar; entra sin
      escribir la contraseña (refresh por cookie).
- [ ] **Instancia única:** lanzar «Asistente» una segunda vez no crea una segunda
      ventana; enfoca/muestra la existente.

## Checklist de release

- [ ] `make widget-package` termina con exit 0 y deja `.deb` + AppImage.
- [ ] `dpkg-deb --info`/`--contents` confirman binario, `.desktop` e iconos.
- [ ] AppImage arranca y el servidor registra `GET /widget` 200.
- [ ] `cargo test` en `desktop/src-tauri` en verde (46 tests).
- [ ] Protocolo de prueba manual completo con el `.deb` instalado.

## Next step

Con el paquete verificado, el siguiente paso es **T7**: botón «Instalar widget» en la
consola web que entrega estos artefactos con instrucciones por SO
(`odd/tasks/desktop-widget.md`).
