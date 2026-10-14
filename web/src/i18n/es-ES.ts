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
    },
    team: {
      title: 'Equipo',
      description: 'Da de alta a las personas de tu empresa y gestiona sus roles.',
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
