import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// Mirrors index.html, which the test document does not load.
document.documentElement.lang = 'es-ES';

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  document.title = '';
});
