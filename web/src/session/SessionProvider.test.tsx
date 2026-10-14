import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it } from 'vitest';
import { LoginPage } from '../pages/LoginPage';
import { RequireAuth } from '../routes/RequireAuth';
import { installFakeApi, signedInRoutes } from '../test/fakeApi';
import { LocationProbe, renderApp } from '../test/renderApp';
import { SessionProvider, useSession } from './SessionProvider';
import { TOKEN_STORAGE_KEY } from './tokenStorage';

/** A protected page that calls the API with the session token, like L3–L5 sections will. */
function ApiProbe() {
  const { api } = useSession();
  return (
    <main>
      <h1>Probe</h1>
      <button type="button" onClick={() => api.get('/api/v1/users').catch(() => undefined)}>
        Load
      </button>
    </main>
  );
}

describe('session', () => {
  it('returns to login with a message when the API answers 401 mid-session', async () => {
    installFakeApi({
      ...signedInRoutes('admin'),
      'GET /api/v1/users': { status: 401, body: { detail: 'invalid token' } },
    });
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, 'admin-token');
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/probe']}>
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

    await user.click(await screen.findByRole('button', { name: 'Load' }));

    expect(await screen.findByText('Tu sesión ha caducado. Vuelve a iniciar sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('treats a stored token the API rejects as an expired session', async () => {
    installFakeApi({ 'GET /api/v1/auth/me': { status: 401, body: { detail: 'invalid token' } } });
    renderApp({ route: '/chat', storedToken: 'stale-token' });

    expect(await screen.findByText('Tu sesión ha caducado. Vuelve a iniciar sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('logs out: forgets the token and confirms on the login page', async () => {
    installFakeApi(signedInRoutes('viewer'));
    const { user } = renderApp({ route: '/chat', storedToken: 'viewer-token' });

    await user.click(await screen.findByRole('button', { name: 'Cerrar sesión' }));

    expect(await screen.findByText('Has cerrado la sesión.')).toHaveAttribute('role', 'status');
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });
});
