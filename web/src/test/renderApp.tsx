import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { vi } from 'vitest';
import { App } from '../App';
import { jsonResponse } from './fakeApi';

/** Renders the current pathname (hidden, outside the a11y tree) so tests can assert redirects. */
export function LocationProbe() {
  const { pathname } = useLocation();
  return (
    <output data-testid="location" hidden>
      {pathname}
    </output>
  );
}

export interface RenderAppOptions {
  /** Initial URL. */
  route?: string;
  /** History state of the initial URL, e.g. `{ from }` as set by the auth guard. */
  state?: unknown;
  /**
   * A restorable session: the fake refresh endpoint answers with this access
   * token, as a browser would when sending back the refresh cookie. Without it
   * the endpoint answers 401 (no session in this browser).
   */
  storedToken?: string;
}

/**
 * Render the real app inside a memory router. The refresh cookie is invisible
 * to JavaScript, so this wrapper stands in for it: `POST /api/v1/auth/refresh`
 * is answered here (200 with `storedToken`, else 401) while every other call
 * goes to the fake API the test installed.
 */
export function renderApp({ route = '/', state, storedToken }: RenderAppOptions = {}) {
  const previousFetch = globalThis.fetch;

  const sessionFetch = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const method = (init.method ?? 'GET').toUpperCase();
    if (method === 'POST' && String(input) === '/api/v1/auth/refresh') {
      return storedToken === undefined
        ? jsonResponse({ status: 401, body: { detail: 'invalid refresh token' } })
        : jsonResponse({
            status: 200,
            body: { access_token: storedToken, token_type: 'bearer', expires_in: 1800 },
          });
    }
    // A test may install its fake API after rendering (e.g. to retry): honour it.
    const currentFetch = globalThis.fetch;
    return (currentFetch === sessionFetch ? previousFetch : currentFetch)(input, init);
  });
  vi.stubGlobal('fetch', sessionFetch);

  const user = userEvent.setup();
  const view = render(
    <MemoryRouter initialEntries={[state === undefined ? route : { pathname: route, state }]}>
      <App />
      <LocationProbe />
    </MemoryRouter>,
  );
  return { user, sessionFetch, ...view };
}
