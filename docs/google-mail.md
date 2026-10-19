# Gmail en el asistente: configuración y uso

El complemento **Gmail** (clave `google_mail`) permite que el agente del chat opere el correo
de cada usuario en su propia cuenta de Gmail: buscar y leer mensajes dentro del alcance que
el usuario eligió y, siempre con aprobación previa, enviar, archivar y etiquetar. La
aplicación web **no muestra el correo**: no hay bandeja ni vista de mensajes; todo ocurre en
la conversación con el asistente. Este documento explica la preparación en Google Cloud, la
activación por parte del administrador, la conexión de cada usuario, qué puede y qué no
puede hacer el agente, las limitaciones conocidas y el protocolo de prueba manual.

> **Privacidad — leer antes de conectar un buzón.**
> Lo que el agente lee (asunto, remitente, extracto y cuerpo) pasa al modelo de lenguaje
> configurado. Con un proveedor externo (`AA_LLM_PROVIDER=openai_compatible`, por ejemplo el
> perfil Grok de [dev-setup.md](dev-setup.md)) ese contenido sale de la máquina hacia el
> proveedor. Para las pruebas usa una cuenta de Gmail creada para ello, nunca un buzón con
> datos reales de clientes.

## Ruta rápida

1. **Google Cloud:** habilita la API de Gmail, declara los dos permisos en la pantalla de
   consentimiento, añade tu cuenta como usuario de prueba y crea un cliente OAuth de tipo
   «Aplicación web» con la URI `http://localhost:8000/api/v1/oauth/google/callback`
   (ver [Requisitos en Google Cloud](#requisitos-en-google-cloud)).
2. **Servidor:** define `AA_GOOGLE_CLIENT_ID`, `AA_GOOGLE_CLIENT_SECRET` y
   `AA_CREDENTIALS_KEY` y reinicia (ver [Variables del servidor](#4-variables-del-servidor)).
3. **Administrador:** Configuración → Herramientas → Gmail → **Activar**.
4. **Usuario:** Configuración → Mis herramientas → Gmail → **Configurar**: Conectar → elegir
   el alcance → Probar conexión.
5. **Chat:** pide, por ejemplo, «Busca los últimos correos de Ana». Las acciones devuelven un
   identificador de aprobación que se aprueba por la API (ver
   [Cómo se aprueba una acción](#cómo-se-aprueba-una-acción)).

## Requisitos en Google Cloud

La consola de Google cambia a menudo los nombres de sus menús (la configuración de OAuth
aparece ahora como «Google Auth Platform»); los pasos describen qué configurar, no la ruta
exacta de clics.

### 1. Proyecto y API de Gmail

- Usa un proyecto de Google Cloud. Puede ser el mismo del complemento de calendario: ambos
  comparten cliente OAuth (ver [Variables del servidor](#4-variables-del-servidor)).
- Habilita **Gmail API** en la biblioteca de APIs del proyecto. Sin ella, el servidor no puede
  leer las etiquetas y el paso de elección de alcance falla.

### 2. Pantalla de consentimiento y permisos

| Ajuste | Valor |
|--------|-------|
| Tipo de usuario | Externo (necesario para cuentas `@gmail.com`) |
| Estado de publicación | Prueba (*Testing*) mientras la aplicación no esté verificada |
| Permisos que solicita el servidor | `https://www.googleapis.com/auth/gmail.modify` y `https://www.googleapis.com/auth/gmail.send` (`app/modules/google_mail/manifest.py`) |
| Usuarios de prueba | Cada cuenta de Gmail que vaya a conectarse |

Si el proyecto también sirve al calendario, declara además
`https://www.googleapis.com/auth/calendar.events` (`app/modules/calendar/manifest.py`).

Google clasifica `gmail.modify` como permiso **restringido** y `gmail.send` como **sensible**
([Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)). Qué
implica:

| Situación | Consecuencia (fuente) |
|-----------|----------------------|
| Modo Prueba | Hasta 100 usuarios de prueba; Google muestra un aviso antes de que un usuario de prueba autorice los permisos; la autorización caduca a los **7 días** del consentimiento ([Manage App Audience](https://support.google.com/cloud/answer/15549945), [OAuth 2.0](https://developers.google.com/identity/protocols/oauth2)). Pasado ese plazo hay que pulsar **Reconectar**. |
| Producción | Los permisos restringidos exigen la verificación de aplicaciones con permisos restringidos y, si los datos se almacenan o se transmiten a través de servidores, una evaluación de seguridad ([Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)). Este backend transmite el correo a través de su servidor y hacia el modelo. **A confirmar con Google** el alcance exacto del proceso antes de abrir la aplicación a usuarios reales. |

### 3. Cliente OAuth y URI de redirección

Crea un «ID de cliente de OAuth» de tipo **Aplicación web** y registra como URI de
redirección autorizada:

```text
{origen}/api/v1/oauth/google/callback
```

El servidor construye esa URI a partir de la petición que recibe
(`request.url_for("google_oauth_callback")` en `app/modules/oauth/router.py`): `{origen}` es
el esquema, el host y el puerto con los que el navegador llega al backend. Google exige que
coincida exactamente (esquema, mayúsculas y barra final) y solo admite `http` para
`localhost` y direcciones IP locales
([OAuth 2.0 for Web Server Applications](https://developers.google.com/identity/protocols/oauth2/web-server)).

| Cómo abres la aplicación | URI que debes registrar |
|--------------------------|-------------------------|
| `make app` y <http://localhost:8000> | `http://localhost:8000/api/v1/oauth/google/callback` |
| <http://127.0.0.1:8000> | `http://127.0.0.1:8000/api/v1/oauth/google/callback` (no es intercambiable con `localhost`) |
| `make web-dev` (Vite en el puerto 5173) | **A confirmar.** El proxy de Vite (`web/vite.config.ts`) reenvía `/api` sin reescribir el host, así que la URI saldría con `:5173`. Para la prueba manual usa `make app`. |
| Producción tras un proxy HTTPS | `https://<dominio>/api/v1/oauth/google/callback`. **A confirmar:** uvicorn debe aceptar las cabeceras del proxy para generar `https`; si no, Google responde `redirect_uri_mismatch`. |

### 4. Variables del servidor

Los ajustes se leen de variables con prefijo `AA_` (`app/core/config.py`); `make dev` y
`make app` cargan además `.env` si existe (`Makefile`).

| Variable | Para qué | Si falta |
|----------|----------|----------|
| `AA_GOOGLE_CLIENT_ID` | ID del cliente OAuth | **Conectar** responde 501 `Google OAuth is not configured; set AA_GOOGLE_CLIENT_ID and AA_GOOGLE_CLIENT_SECRET`. |
| `AA_GOOGLE_CLIENT_SECRET` | Secreto del cliente OAuth (solo en el servidor: no aparece en la URL de consentimiento ni se guarda con los tokens) | Igual que la anterior; además, un token caducado no se puede renovar y el agente pide reconectar. |
| `AA_CREDENTIALS_KEY` | Clave Fernet que cifra los tokens de cada usuario en la base de datos | El retorno desde Google responde 500 `credentials encryption is not configured` y no guarda nada. Con `AA_ENVIRONMENT=production` el servidor no arranca. |

Genera la clave con:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Cambiar `AA_CREDENTIALS_KEY` después deja ilegibles las conexiones guardadas: el agente
responde «not configured» y cada usuario debe pulsar **Reconectar**.

El calendario usa las mismas dos variables `AA_GOOGLE_*` y la misma URI de retorno: un único
cliente OAuth sirve para los dos complementos.

## Administración: activar el complemento

Solo los roles Propietario y Administrador ven **Configuración → Herramientas**
(`/configuracion/herramientas`).

1. Localiza la fila **Gmail**; muestra sus herramientas y sus permisos.
2. Pulsa **Activar** y confirma. El cambio se aplica a toda la empresa.
3. Las personas de la empresa ven Gmail en **Mis herramientas** con el estado «Sin configurar».

**Desactivar** retira las herramientas de Gmail al agente; una acción ya aprobada de un
módulo desactivado se rechaza al ejecutarla. La configuración y los datos de cada usuario se
conservan. Equivalente por API: `POST` / `DELETE /api/v1/modules/google_mail/activate`.

## Usuario: conectar la cuenta y elegir el alcance

Cualquier rol puede configurar su propia cuenta en **Configuración → Mis herramientas**
(`/configuracion/mis-herramientas`) → Gmail → **Configurar**, que abre el asistente de
configuración en `/configuracion/herramientas/google_mail/onboarding`.

| Paso (título en pantalla) | Qué haces | Qué ocurre |
|---------------------------|-----------|------------|
| 1. «Connect with Google» | Pulsa **Conectar**. | Google pide elegir la cuenta y aceptar los permisos. En modo Prueba muestra antes un aviso de aplicación no verificada: continúa. Si Google ofrece una casilla por permiso, marca las dos. Al aceptar vuelves al asistente. |
| 2. «Choose the mailbox scope» | Marca **una sola** opción y pulsa **Guardar y continuar**. | Opciones: «All mail» (todo el buzón), «Inbox» (Recibidos) y cada etiqueta que hayas creado, con su nombre. Las demás etiquetas de sistema (Enviados, Destacados…) no se ofrecen. |
| 3. «Verify the connection» | Pulsa **Probar conexión**. | El servidor lee tu perfil de Gmail; si responde, aparece «Configuración completada». La pantalla no muestra la dirección conectada. |

Al terminar, Mis herramientas muestra Gmail como «Conectado».

### Cambiar el alcance o desconectar

- **No hay edición del alcance.** Abre directamente
  `/configuracion/herramientas/google_mail/onboarding` (Mis herramientas solo ofrece
  **Configurar** mientras la herramienta está sin configurar), pulsa **Reconectar** y
  confirma. Se borran la elección de alcance y los tokens guardados, y repites los tres
  pasos (`restart` en `app/modules/onboarding/service.py`). Hasta que termines, el agente
  responde «not configured».
- **Reconectar no revoca el acceso en Google.** Para retirarlo también allí, elimina la
  aplicación de la lista de aplicaciones de terceros con acceso, en la configuración de
  seguridad de tu cuenta de Google.

## Qué puede hacer el agente

| Herramienta | Qué hace | Aprobación | Rol mínimo | Alcance |
|-------------|----------|------------|------------|---------|
| `search_emails` | Busca con texto libre opcional (de 1 a 50 resultados, 10 por defecto) y devuelve de cada mensaje su ID, ID de hilo, etiquetas, asunto, remitente, fecha y extracto | No | Lector | Solo mensajes con la etiqueta elegida (salvo «All mail») |
| `get_email` | Lee un mensaje completo: destinatarios y cuerpo en texto plano (hasta 20 000 caracteres) | No | Lector | Rechaza los mensajes fuera del alcance |
| `list_labels` | Lista las etiquetas del buzón (ID y nombre) | No | Lector | Todas las etiquetas, también con alcance de una sola: solo nombres, nunca mensajes |
| `send_email` | Envía un correo de texto: de 1 a 20 destinatarios, CC/CCO, asunto, cuerpo y, para responder, el ID del hilo; el remitente es siempre la cuenta conectada | **Siempre** | Operador | Exento del filtro de etiqueta: un correo nuevo no pertenece a ninguna |
| `archive_message` | Archiva un mensaje (le quita la etiqueta `INBOX`) | **Siempre** | Operador | Solo mensajes dentro del alcance |
| `add_label` | Añade una etiqueta a un mensaje | **Siempre** | Operador | El mensaje debe estar dentro del alcance; la etiqueta puede ser cualquiera salvo `TRASH` y `SPAM` |

- **Leer no pide confirmación; actuar, siempre.** El agente solo ofrece a cada persona las
  herramientas que su rol permite: un Lector puede leer pero no enviar, archivar ni etiquetar.
- **No hay borrado.** Ninguna herramienta borra ni mueve a la papelera, y `add_label`
  rechaza `TRASH` y `SPAM` antes de crear la aprobación (Gmail vacía ambas a los 30 días).
- **El alcance lo aplica nuestro backend.** Con `gmail.modify`, Google concede acceso al
  buzón completo; no existen permisos por etiqueta. Por eso el servidor filtra cada búsqueda
  por la etiqueta elegida y descarta cualquier resultado que no la tenga, y `get_email`,
  `archive_message` y `add_label` comprueban las etiquetas del mensaje antes de actuar. La
  comprobación se repite justo antes de ejecutar una acción aprobada: aprobar nunca amplía
  el alcance.
- Con alcance «Inbox», un mensaje archivado deja de estar en Recibidos y, por tanto, sale del
  alcance: el agente ya no lo ve.

Mensajes de rechazo que el agente transmite (en inglés, `app/modules/google_mail/tools.py`):

| Texto que empieza por | Causa | Qué hacer |
|-----------------------|-------|-----------|
| `google mail connection not configured` | No hay conexión, no hay alcance guardado o los tokens no se pueden descifrar | Completa los tres pasos de configuración |
| `google mail connection expired` | El token caducó y no se pudo renovar, o Google rechazó el token al comprobar un mensaje | **Reconectar** |
| `message is outside the mailbox scope` | El mensaje no tiene la etiqueta elegida | Reconectar con un alcance más amplio, si procede |

### Cómo se aprueba una acción

Cuando el agente pide una acción, el chat muestra «Esta acción necesita aprobación y no se
ejecutará hasta que una persona autorizada la apruebe. Identificador de la aprobación: …» y
la fecha de caducidad (24 horas por defecto, `AA_APPROVAL_TTL_SECONDS`). La aplicación web
todavía **no tiene panel de aprobaciones**: se aprueba por la API. Puede aprobar cualquier
persona de la empresa con rol Operador o superior, incluida la que hizo la petición.

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@demo.test","password":"demo-admin-pass","tenant_slug":"demo"}' \
  | jq -r .access_token)
ID=<identificador de la aprobación>

# 1. Revisar exactamente qué se va a ejecutar
curl -s localhost:8000/api/v1/approvals/$ID -H "Authorization: Bearer $TOKEN" \
  | jq '{tool_name, status, input}'

# 2a. Aprobar y ejecutar (una sola vez)
curl -s -X POST localhost:8000/api/v1/approvals/$ID/approve -H "Authorization: Bearer $TOKEN" | jq .status
curl -s -X POST localhost:8000/api/v1/approvals/$ID/execute -H "Authorization: Bearer $TOKEN" \
  | jq '{status, execution_result, execution_error}'

# 2b. O rechazar (motivo opcional)
curl -s -X POST localhost:8000/api/v1/approvals/$ID/reject -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"reason":"no enviar"}' | jq .status
```

`GET /api/v1/approvals?status=pending` lista las pendientes. El resultado de `execute`
(`executed` o `failed` con `execution_error`) **no vuelve al chat**: el agente no sabe si la
acción se completó.

> **Ejecuta con la misma cuenta que pidió la acción.** `execute` ejecuta la herramienta con
> la persona que llama a ese endpoint (`execute_approved_action` en
> `app/modules/approvals/service.py`): si otra persona ejecuta la aprobación, se usan **su**
> conexión de Gmail y **su** alcance. Ver [Limitaciones conocidas](#limitaciones-conocidas).

## Limitaciones conocidas

| Limitación | Detalle |
|------------|---------|
| Una sola etiqueta | El alcance es todo el buzón, Recibidos o **una** etiqueta de usuario; no se pueden combinar varias. |
| Textos en inglés en la configuración | Las opciones «All mail» e «Inbox» y los títulos y descripciones de los pasos llegan del servidor en inglés. |
| Casillas en lugar de opción única | El selector de alcance muestra casillas aunque solo admite una opción: si marcas varias, el servidor rechaza el envío y la página muestra «Los datos no son válidos. Revisa los campos e inténtalo de nuevo.». |
| Etiqueta nombrada por su ID | Cuando el agente habla del alcance, nombra una etiqueta de usuario por su ID (`Label_…`), no por su nombre. |
| Token revocado en Google | La comprobación previa solo mira la caducidad guardada. Si revocas el acceso desde Google, `search_emails`, `list_labels` y `send_email` fallan con un error genérico en lugar de pedir reconectar; `get_email`, `archive_message` y `add_label` sí piden reconectar. En ambos casos la solución es **Reconectar**. |
| Permisos concedidos a medias | El servidor no comprueba qué permisos aceptó el usuario. Si desmarcas uno en la pantalla de Google, las herramientas que lo necesitan fallan (error genérico o «reconectar»). |
| Respuestas en el hilo | `send_email` solo envía el ID del hilo. Google exige además las cabeceras `References` e `In-Reply-To` y un asunto coincidente para agrupar una respuesta ([Managing threads](https://developers.google.com/workspace/gmail/api/guides/threads)), así que la respuesta **puede no aparecer en el hilo**. Se comprueba en la prueba manual. |
| Cambiar el alcance | Solo con **Reconectar**, que obliga a repetir la conexión con Google. |
| Mensaje inexistente | `get_email` con un ID que no existe devuelve un error genérico, no «no existe». |
| Correos solo en HTML | El agente solo lee la parte de texto plano: de un correo solo en HTML ve el asunto y el extracto, pero no el cuerpo. |
| Aprobaciones solo por API | Sin panel en la web; el resultado no vuelve al chat; quien ejecuta la aprobación aporta su propia conexión de Gmail. |
| Modo Prueba de Google | La autorización caduca a los 7 días: el agente pide reconectar. |
| Conexión en curso | El estado del inicio de sesión con Google vive en la memoria del proceso y caduca a los 10 minutos: si el servidor se reinicia (por ejemplo, `--reload` tras editar un archivo) entre **Conectar** y la vuelta desde Google, el retorno responde 400 `invalid or expired OAuth state`. Repite **Conectar**. |

## Protocolo de prueba manual

Lo ejecuta una persona con una cuenta de Gmail real de prueba. Marca cada paso al superarlo y
anota el resultado del paso 13.

**Preparación**

- Servidor con `make app` en <http://localhost:8000>, la URI de retorno de `localhost:8000`
  registrada en Google y sesión iniciada en la web como `admin@demo.test` (empresa `demo`).
- Un terminal con `TOKEN` del mismo usuario (ver
  [Cómo se aprueba una acción](#cómo-se-aprueba-una-acción)). Todas las ejecuciones se hacen
  con ese usuario.
- En la cuenta de Gmail de prueba: una etiqueta de usuario **Asistente** y tres correos
  recibidos desde una segunda dirección que controles:
  - M1, asunto `Prueba-Asistente-1`, con la etiqueta Asistente;
  - M2, asunto `Prueba-Fuera-1`, sin la etiqueta;
  - M3, asunto `Prueba-Archivar-1`, sin la etiqueta.

**A. Activación y conexión (alcance «All mail»)**

1. [ ] **Activar:** en Herramientas, **Activar** Gmail. *Esperado:* estado «Activo»; en Mis
   herramientas, Gmail aparece «Sin configurar».
2. [ ] **Conectar:** **Configurar** → **Conectar**. *Esperado:* Google muestra el aviso de
   aplicación no verificada y los permisos de lectura/modificación y de envío; tras aceptar
   vuelves al paso 2 del asistente.
3. [ ] **Opciones de alcance:** *Esperado:* «All mail», «Inbox» y «Asistente» por su nombre,
   sin IDs `Label_…`. Marca dos opciones y guarda. *Esperado:* error de validación, no se
   avanza.
4. [ ] **Guardar y probar:** marca solo «All mail», **Guardar y continuar**, **Probar
   conexión**. *Esperado:* «Configuración completada»; Mis herramientas muestra «Conectado».

**B. Lectura sin aprobación**

5. [ ] **Buscar:** «Busca los correos con asunto Prueba-Fuera-1 y Prueba-Asistente-1 y dime sus
   IDs y el ID de hilo de cada uno». *Esperado:* ambos con asunto, remitente y fecha; ningún
   aviso de aprobación. Anota los IDs de M1, M2 y M3 y el ID de hilo de M1.
6. [ ] **Leer:** «Lee el cuerpo completo de Prueba-Fuera-1». *Esperado:* el cuerpo de M2, sin
   aprobación.
7. [ ] **Etiquetas:** «¿Qué etiquetas tiene mi correo?». *Esperado:* la lista incluye
   Asistente.

**C. Acciones con aprobación**

8. [ ] **Enviar:** «Envía a <segunda dirección> un correo con asunto Prueba-Envio-1 y cuerpo
   Hola». *Esperado:* aviso con identificador de aprobación; en Gmail, Enviados aún no tiene
   el correo; `GET` de la aprobación muestra `tool_name: send_email` y el `input` pedido.
   Aprueba y ejecuta. *Esperado:* `status: executed`; el correo llega a la segunda dirección
   desde la cuenta conectada.
9. [ ] **Rechazar no ejecuta:** pide otro envío (`Prueba-Rechazo-1`) y **rechaza** la
   aprobación; después llama a `execute`. *Esperado:* `reject` → `rejected`; `execute` → 409;
   el correo no aparece en Enviados ni llega.
10. [ ] **Archivar:** «Archiva Prueba-Archivar-1». Aprueba y ejecuta. *Esperado:*
    `executed`; M3 sale de Recibidos y sigue en Todos.
11. [ ] **Etiquetar:** «Añade la etiqueta Asistente a Prueba-Archivar-1». Aprueba y ejecuta.
    *Esperado:* `executed`; M3 tiene la etiqueta Asistente.
12. [ ] **Papelera y spam:** «Mueve Prueba-Fuera-1 a la papelera» y «Añade la etiqueta SPAM a
    Prueba-Fuera-1». *Esperado:* el agente responde que no puede; **no** aparece aviso de
    aprobación; M2 sigue intacto.
13. [ ] **Responder en el hilo:** «Responde al hilo de Prueba-Asistente-1 diciendo Recibido».
    Aprueba y ejecuta. *Esperado:* `execution_result.thread_id` igual al ID de hilo de M1.
    **Anota** si la respuesta aparece dentro de la conversación de M1 en la cuenta conectada y
    en la segunda dirección (ver [Limitaciones conocidas](#limitaciones-conocidas)).

**D. Alcance «Asistente» (casos negativos)**

14. [ ] **Cambiar el alcance:** abre `/configuracion/herramientas/google_mail/onboarding` →
    **Reconectar** → confirma; repite los tres pasos eligiendo «Asistente». *Esperado:*
    «Configuración completada».
15. [ ] **Búsqueda fuera de la etiqueta:** «Busca Prueba-Fuera-1». *Esperado:* ningún
    resultado. «Busca mis últimos correos». *Esperado:* solo mensajes con la etiqueta
    Asistente (M1, M3).
16. [ ] **Lectura fuera del alcance:** «Lee el mensaje con ID <ID de M2>». *Esperado:* el
    agente transmite el rechazo `message is outside the mailbox scope`; no muestra el cuerpo.
17. [ ] **Lectura dentro del alcance:** «Lee el mensaje con ID <ID de M1>». *Esperado:* el
    cuerpo de M1.
18. [ ] **Archivar y etiquetar fuera del alcance:** «Archiva el mensaje <ID de M2>» y «Añade la
    etiqueta Asistente al mensaje <ID de M2>». *Esperado:* rechazo por alcance **sin** aviso
    de aprobación; M2 sin cambios.
19. [ ] **La aprobación no amplía el alcance:** «Archiva el mensaje <ID de M1>» → aviso de
    aprobación. Antes de aprobar, quita la etiqueta Asistente a M1 en Gmail. Aprueba y ejecuta.
    *Esperado:* `status: failed` con `execution_error` que contiene
    `message is outside the mailbox scope`; M1 sigue en Recibidos.
20. [ ] **Enviar sigue disponible:** «Envía a <segunda dirección> un correo con asunto
    Prueba-Envio-2». *Esperado:* pide aprobación; tras aprobar y ejecutar, el correo llega.

**E. Sin conexión**

21. [ ] **Desconectado:** **Reconectar** → confirma y no completes el paso 1. En el chat:
    «Busca mis correos». *Esperado:* el agente transmite
    `google mail connection not configured`; sin error 500; Mis herramientas muestra «Sin
    configurar».
22. [ ] **Restablecer:** completa de nuevo los tres pasos con el alcance que prefieras.

## Siguiente paso

Registra el resultado del protocolo (incluida la observación del paso 13) en la tarea G5 de
[`odd/tasks/google-mail.md`](../odd/tasks/google-mail.md).
