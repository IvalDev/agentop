/**
 * Spanish (Spain) catalog: the source locale and the shape every other catalog follows.
 * Register: neutral and professional, "tú" or impersonal forms, Spain vocabulary.
 * Placeholders use `{name}` and are filled by `t(key, { name })`.
 */
export const esES = {
  app: {
    name: 'Asistente de trabajo',
    pageTitle: '{page} · {app}',
    skipToContent: 'Saltar al contenido principal',
    loading: 'Cargando…',
  },
  roles: {
    owner: 'Propietario',
    admin: 'Administrador',
    operator: 'Operador',
    viewer: 'Lector',
  },
  login: {
    title: 'Iniciar sesión',
    intro: 'Accede con los datos que te ha facilitado tu empresa.',
    tenantLabel: 'Empresa',
    tenantHint: 'Identificador de tu empresa, por ejemplo «demo».',
    emailLabel: 'Correo electrónico',
    passwordLabel: 'Contraseña',
    submit: 'Iniciar sesión',
    submitting: 'Iniciando sesión…',
    errors: {
      tenantRequired: 'Indica el identificador de tu empresa.',
      emailRequired: 'Indica tu correo electrónico.',
      emailInvalid: 'Introduce un correo electrónico válido, por ejemplo nombre@empresa.es.',
      passwordRequired: 'Indica tu contraseña.',
      invalidCredentials: 'La empresa, el correo electrónico o la contraseña no son correctos.',
      network: 'No se ha podido conectar con el servidor. Comprueba la conexión e inténtalo de nuevo.',
      unexpected: 'Se ha producido un error inesperado. Inténtalo de nuevo.',
    },
    notices: {
      expired: 'Tu sesión ha caducado. Vuelve a iniciar sesión.',
      loggedOut: 'Has cerrado la sesión.',
    },
  },
  session: {
    unavailable: {
      title: 'No se ha podido recuperar tu sesión',
      message: 'No se ha podido comprobar tu sesión con el servidor. Comprueba la conexión e inténtalo de nuevo.',
      retry: 'Reintentar',
    },
  },
  header: {
    signedInAs: 'Sesión iniciada como',
    logout: 'Cerrar sesión',
  },
  nav: {
    label: 'Principal',
    toggle: 'Menú',
    chat: 'Chat',
    knowledge: 'Base de conocimiento',
    settings: 'Configuración',
    team: 'Equipo',
  },
  sections: {
    comingSoon: 'Esta sección estará disponible próximamente.',
    chat: {
      title: 'Chat',
      description: 'Conversa con el asistente sobre la información de tu empresa.',
    },
    knowledge: {
      title: 'Base de conocimiento',
      description: 'Consulta los documentos que el asistente utiliza para responder y añade otros nuevos.',
      descriptionReadOnly: 'Consulta los documentos que el asistente utiliza para responder.',
    },
    team: {
      title: 'Equipo',
      description: 'Da de alta a las personas de tu empresa y gestiona sus roles.',
    },
  },
  chat: {
    logLabel: 'Conversación',
    empty: 'Todavía no hay mensajes. Escribe tu pregunta para empezar.',
    you: 'Tú',
    assistant: 'Asistente',
    messageLabel: 'Mensaje',
    messageHint: 'Pulsa Intro para enviar y Mayús + Intro para añadir una línea.',
    messageRequired: 'Escribe un mensaje antes de enviarlo.',
    send: 'Enviar',
    stop: 'Detener',
    reset: 'Nueva conversación',
    resetDone: 'Has empezado una conversación nueva.',
    activity: {
      thinking: 'Preparando la respuesta…',
      searching: 'Buscando en la base de conocimiento…',
      usingTool: 'Consultando una herramienta…',
    },
    sources: 'Fuentes',
    sourceSummary: '[{number}] {document}',
    sourceLink: '[{number}] Fuente: {document}',
    approval:
      'Esta acción necesita aprobación y no se ejecutará hasta que una persona autorizada la apruebe. ' +
      'Identificador de la aprobación: {id}. Caduca el {expiresAt}.',
    answerAnnouncement: 'Respuesta del asistente: {answer}',
    notices: {
      stepLimit:
        'El asistente no ha podido terminar la respuesta en el número de pasos permitido. ' +
        'Concreta la pregunta o inténtalo de nuevo.',
      stopped: 'Has detenido la respuesta.',
    },
    errors: {
      modelUnavailable: 'El asistente no está disponible en este momento. Inténtalo de nuevo más tarde.',
      network: 'No se ha podido conectar con el servidor. Comprueba la conexión e inténtalo de nuevo.',
      interrupted: 'La respuesta se ha interrumpido. Inténtalo de nuevo.',
      invalidMessage: 'No se ha podido enviar el mensaje. Revisa su longitud e inténtalo de nuevo.',
      unexpected: 'Se ha producido un error inesperado. Inténtalo de nuevo.',
    },
  },
  knowledge: {
    upload: {
      title: 'Añadir documentos',
      filesLabel: 'Archivos',
      filesHint:
        'Formatos admitidos: .md, .txt, .pdf y .docx, de hasta 20 MB cada uno. ' +
        'También puedes arrastrar los archivos a esta zona.',
      filesRequired: 'Selecciona al menos un archivo.',
      submit: 'Subir y procesar',
      progressTitle: 'Progreso de la subida',
      progress: '{name}: {state}',
      finished: 'Subida terminada. Archivos listos: {done}. Con errores: {failed}.',
      state: {
        pending: 'En espera',
        uploading: 'Subiendo…',
        processing: 'Procesando…',
        done: 'Listo',
        failed: 'Error: {reason}',
      },
    },
    /** Why a file could not be added; shown after "Error:" or a colon, so lowercase. */
    errors: {
      tooLarge: 'el archivo supera el límite de 20 MB.',
      unsupportedType: 'el formato no está admitido. Usa archivos .md, .txt, .pdf o .docx.',
      invalidName: 'el nombre del archivo no es válido.',
      unreadable: 'no se ha podido leer el contenido del archivo o su formato no está admitido.',
      processingUnavailable:
        'el servicio que procesa los documentos no está disponible. Inténtalo de nuevo más tarde.',
      forbidden: 'no tienes permiso para añadir o procesar documentos.',
      notFound: 'el documento ya no existe.',
      network: 'no se ha podido conectar con el servidor. Comprueba la conexión e inténtalo de nuevo.',
      unexpected: 'se ha producido un error inesperado. Inténtalo de nuevo.',
    },
    list: {
      title: 'Documentos',
      caption: 'Documentos de la base de conocimiento',
      refresh: 'Actualizar',
      loading: 'Cargando documentos…',
      loadError: 'No se han podido cargar los documentos. Comprueba la conexión y pulsa «Actualizar».',
      empty: 'Todavía no hay documentos en la base de conocimiento.',
      columns: {
        name: 'Nombre',
        type: 'Tipo',
        size: 'Tamaño',
        date: 'Fecha',
        status: 'Estado',
        actions: 'Acciones',
      },
      retry: 'Reintentar',
      process: 'Procesar',
      actionTarget: '«{name}»',
      technicalDetail: 'Detalle técnico',
    },
    status: {
      uploaded: 'Sin procesar',
      processing: 'Procesando',
      processed: 'Procesado',
      failed: 'Error',
    },
    failure: {
      unsupported: 'El formato del archivo no está admitido.',
      embeddings: 'El servicio que prepara los documentos para las búsquedas no estaba disponible.',
      generic: 'No se ha podido procesar el documento.',
    },
    types: {
      markdown: 'Markdown',
      text: 'Texto',
      pdf: 'PDF',
      word: 'Word',
      other: 'Otro',
    },
    ingest: {
      started: 'Procesando «{name}»…',
      done: '«{name}» se ha procesado correctamente.',
      failed: 'No se ha podido procesar «{name}»: {reason}',
    },
  },
  notFound: {
    title: 'Página no encontrada',
    description: 'La dirección que has abierto no existe.',
    backToChat: 'Ir al chat',
  },
};

/** The catalog shape; other locales must provide every key. */
export type Messages = typeof esES;
