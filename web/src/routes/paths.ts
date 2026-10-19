/** Client-side routes. FastAPI serves `index.html` for all of them (SPA fallback). */
export const paths = {
  login: '/login',
  /** Chat-only shell loaded by the desktop widget, outside the dashboard layout. */
  widget: '/widget',
  chat: '/chat',
  knowledge: '/conocimiento',
  settings: '/configuracion',
  team: '/configuracion/equipo',
  tools: '/configuracion/herramientas',
  myTools: '/configuracion/mis-herramientas',
  /** The onboarding wizard route pattern (`:module` is the module key). */
  onboardingRoute: '/configuracion/herramientas/:module/onboarding',
  /** The onboarding wizard for one module — where the OAuth callback lands (D8). */
  onboarding: (module: string) => `/configuracion/herramientas/${encodeURIComponent(module)}/onboarding`,
  /** Install the desktop widget (T7): every role, outside the Settings admin gate. */
  installWidget: '/instalar-widget',
} as const;
