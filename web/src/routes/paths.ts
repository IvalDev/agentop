/** Client-side routes. FastAPI serves `index.html` for all of them (SPA fallback). */
export const paths = {
  login: '/login',
  chat: '/chat',
  knowledge: '/conocimiento',
  settings: '/configuracion',
  team: '/configuracion/equipo',
} as const;
