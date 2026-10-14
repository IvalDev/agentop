import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The API runs on localhost:8000 (`make dev`); the dev server forwards `/api` to it so the
// browser talks to a single origin, exactly like the built app served by FastAPI.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
