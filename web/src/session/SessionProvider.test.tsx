import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { LoginPage } from '../pages/LoginPage';
import { RequireAuth } from '../routes/RequireAuth';
import { expectNoAxeViolations } from '../test/axe';
import {
  createGate,
  headersOf,
  installFakeApi,
  meBody,
  routesCalled,
  signedInRoutes,
} from '../test/fakeApi';
import { LocationProbe, renderApp } from '../test/renderApp';
import { SessionProvider, useSession } from './SessionProvider';
import { memoryTokenStorage } from './tokenStorage';

const REFRESH = 'POST /api/v1/auth/refresh';
const ME = 'GET /api/v1/auth/me';

/** A protected page that calls the API with the session token, like L3–L5 sections will. */
function ApiProbe() {
  const { api } = useSession();
  const [loaded, setLoaded] = useState(0);
  return (
    <main>
      <h1>Probe</h1>
      <button
        type="button"
        onClick={() => {
          void api.get('/api/v1/users').then(
            () => setLoaded((count) => count + 1),
            () => undefined,
          );
        }}
      >
        Load
      </button>
      <output data-testid="loaded">{loaded}</output>
    </main>
  );
}

/** The provider under test, with login and a protected probe route. */
function renderSession(initialEntries: string[] = ['/probe']) {
  const user = userEvent.setup();
  const view = render(
    <MemoryRouter initialEntries={initialEntries}>
      <SessionProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/probe"
            element={
              <RequireAuth>
                <ApiProbe />
              </RequireAuth>
            }
          />
        </Routes>
        <LocationProbe />
      </SessionProvider>
    </MemoryRouter>,
  );
  return { user, ...view };
}

const REFRESH_OK = {
  status: 200,
  body: { access_token: 'restored-token', token_type: 'bearer', expires_in: 1800 },
};

describe('session', () => {
  it('restores the session silently from the refresh cookie', async () => {
    const fetchMock = installFakeApi({
      [REFRESH]: REFRESH_OK,
      [ME]: { status: 200, body: meBody('viewer') },
    });
    renderSession();

    expect(await screen.findByRole('button', { name: 'Load' })).toBeInTheDocument();

    const refreshCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/auth/refresh');
    expect(refreshCall).toBeDefined();
    expect(headersOf(refreshCall?.[1]).has('Authorization')).toBe(false);
    expect(memoryTokenStorage.read()).toBe('restored-token');
    expect(window.sessionStorage.length).toBe(0);
  });

  it('cold-starts anonymously, without a notice, when the cookie is rejected', async () => {
    installFakeApi({ [REFRESH]: { status: 401, body: { detail: 'invalid refresh token' } } });
    renderSession();

    expect(await screen.findByRole('heading', { level: 1, name: 'Iniciar sesión' })).toBeInTheDocument();
    expect(screen.queryByText(/Tu sesión ha caducado/)).not.toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    expect(window.sessionStorage.length).toBe(0);
  });

  it('shows the retry screen on a transient failure and restores on retry', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const { user } = renderSession();

    expect(await screen.findByRole('alert')).toHaveTextContent(/No se ha podido comprobar tu sesión/);
    expect(screen.getByTestId('location')).toHaveTextContent('/probe');
    // The screen announces itself in the page title; axe checks that title below.
    await vi.waitFor(() => {
      expect(document.title).toBe('No se ha podido recuperar tu sesión · Asistente de trabajo');
    });
    await expectNoAxeViolations();

    installFakeApi({ [REFRESH]: REFRESH_OK, [ME]: { status: 200, body: meBody('viewer') } });
    await user.click(screen.getByRole('button', { name: 'Reintentar' }));

    expect(await screen.findByRole('button', { name: 'Load' })).toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
  });

  it('retries a 401 once and returns to login as expired if the retry is also rejected', async () => {
    installFakeApi({
      [REFRESH]: REFRESH_OK,
      [ME]: { status: 200, body: meBody('admin') },
      'GET /api/v1/users': { status: 401, body: { detail: 'invalid token' } },
    });
    const { user } = renderSession();

    await user.click(await screen.findByRole('button', { name: 'Load' }));

    expect(await screen.findByText('Tu sesión ha caducado. Vuelve a iniciar sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    expect(window.sessionStorage.length).toBe(0);
  });

  it('shares a single refresh between concurrent 401s and retries both', async () => {
    let usersCalls = 0;
    let refreshCalls = 0;
    const gate = createGate();
    const fetchMock = installFakeApi({
      'GET /api/v1/users': () =>
        (usersCalls += 1) <= 2
          ? { status: 401, body: { detail: 'invalid token' } }
          : { status: 200, body: { ok: true } },
      [REFRESH]: () => {
        refreshCalls += 1;
        if (refreshCalls === 1) {
          return REFRESH_OK;
        }
        return gate.hold(() => ({
          status: 200,
          body: { access_token: 'shared-token', token_type: 'bearer', expires_in: 1800 },
        }));
      },
      [ME]: { status: 200, body: meBody('viewer') },
    });
    const { user } = renderSession();
    const load = await screen.findByRole('button', { name: 'Load' });

    await user.click(load);
    await user.click(load);
    await vi.waitFor(() => {
      expect(refreshCalls).toBe(2);
    });

    gate.release();

    await vi.waitFor(() => {
      expect(screen.getByTestId('loaded')).toHaveTextContent('2');
    });
    expect(usersCalls).toBe(4);
    expect(refreshCalls).toBe(2);
    const retries = fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/users').slice(2);
    expect(retries).toHaveLength(2);
    expect(headersOf(retries[0]?.[1]).get('Authorization')).toBe('Bearer shared-token');
  });

  it('returns to login as expired when a mid-session refresh is rejected', async () => {
    let refreshCalls = 0;
    installFakeApi({
      [REFRESH]: () => {
        refreshCalls += 1;
        return refreshCalls === 1
          ? REFRESH_OK
          : { status: 401, body: { detail: 'invalid refresh token' } };
      },
      [ME]: { status: 200, body: meBody('viewer') },
      'GET /api/v1/users': { status: 401, body: { detail: 'invalid token' } },
    });
    const { user } = renderSession();

    await user.click(await screen.findByRole('button', { name: 'Load' }));

    expect(await screen.findByText('Tu sesión ha caducado. Vuelve a iniciar sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
  });

  it('keeps the access token in memory only after login', async () => {
    installFakeApi({
      'POST /api/v1/auth/login': {
        status: 200,
        body: { access_token: 'fresh-token', token_type: 'bearer', expires_in: 1800 },
      },
      [ME]: { status: 200, body: meBody('admin') },
    });
    const { user } = renderApp({ route: '/login' });

    await user.type(await screen.findByLabelText('Correo electrónico'), 'admin@demo.test');
    await user.type(screen.getByLabelText('Contraseña'), 'demo-admin-pass');
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
    expect(memoryTokenStorage.read()).toBe('fresh-token');
  });

  it('lets you leave a session that cannot be checked', async () => {
    installFakeApi({
      [ME]: { status: 502, body: null },
      'POST /api/v1/auth/logout': { status: 204 },
    });
    const { user } = renderApp({ route: '/login', storedToken: 'admin-token' });

    await user.click(await screen.findByRole('button', { name: 'Cerrar sesión' }));

    expect(await screen.findByText('Has cerrado la sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByLabelText('Correo electrónico')).toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
  });

  it('logs out through the server and confirms on the login page', async () => {
    const fetchMock = installFakeApi({
      ...signedInRoutes('viewer'),
      'POST /api/v1/auth/logout': { status: 204 },
    });
    const { user } = renderApp({ route: '/chat', storedToken: 'viewer-token' });

    await user.click(await screen.findByRole('button', { name: 'Cerrar sesión' }));

    expect(await screen.findByText('Has cerrado la sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    await vi.waitFor(() => {
      expect(routesCalled(fetchMock)).toContain('POST /api/v1/auth/logout');
    });
    expect(window.sessionStorage.length).toBe(0);
  });

  it('logs out locally even when the server call fails', async () => {
    installFakeApi({
      ...signedInRoutes('viewer'),
      'POST /api/v1/auth/logout': { status: 503, body: { detail: 'database error' } },
    });
    const { user } = renderApp({ route: '/chat', storedToken: 'viewer-token' });

    await user.click(await screen.findByRole('button', { name: 'Cerrar sesión' }));

    expect(await screen.findByText('Has cerrado la sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
  });
});
