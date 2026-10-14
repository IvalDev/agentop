import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { installFakeApi, meBody } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

const LOGIN = 'POST /api/v1/auth/login';
const ME = 'GET /api/v1/auth/me';

function loginOk() {
  return {
    status: 200,
    body: {
      access_token: 'fresh-token',
      token_type: 'bearer',
      expires_in: 1800,
      user: { id: 1, email: 'admin@demo.test', role: 'admin', tenant_id: 1 },
    },
  };
}

describe('LoginPage', () => {
  it('starts with the demo company and labelled fields', () => {
    installFakeApi({});
    renderApp({ route: '/login' });

    expect(screen.getByRole('heading', { level: 1, name: 'Iniciar sesión' })).toBeInTheDocument();
    expect(screen.getByLabelText('Empresa')).toHaveValue('demo');
    expect(screen.getByLabelText('Correo electrónico')).toHaveValue('');
    expect(screen.getByLabelText('Contraseña')).toHaveAttribute('type', 'password');
    expect(document.title).toBe('Iniciar sesión · Asistente de trabajo');
  });

  it('flags empty fields, focuses the first one, and sends nothing', async () => {
    const fetchMock = installFakeApi({});
    const { user } = renderApp({ route: '/login' });

    await user.clear(screen.getByLabelText('Empresa'));
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));

    const tenant = screen.getByLabelText('Empresa');
    expect(tenant).toHaveAttribute('aria-invalid', 'true');
    expect(tenant).toHaveAccessibleDescription(/Indica el identificador de tu empresa/);
    expect(screen.getByLabelText('Correo electrónico')).toHaveAccessibleDescription(
      'Indica tu correo electrónico.',
    );
    expect(screen.getByLabelText('Contraseña')).toHaveAccessibleDescription('Indica tu contraseña.');
    expect(tenant).toHaveFocus();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects a malformed email', async () => {
    const fetchMock = installFakeApi({});
    const { user } = renderApp({ route: '/login' });

    await user.type(screen.getByLabelText('Correo electrónico'), 'not-an-email');
    await user.type(screen.getByLabelText('Contraseña'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));

    const email = screen.getByLabelText('Correo electrónico');
    expect(email).toHaveAttribute('aria-invalid', 'true');
    expect(email).toHaveAccessibleDescription(/Introduce un correo electrónico válido/);
    expect(email).toHaveFocus();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('trims and lowercases the email, then opens the chat', async () => {
    const fetchMock = installFakeApi({ [LOGIN]: loginOk(), [ME]: { status: 200, body: meBody('admin') } });
    const { user } = renderApp({ route: '/login' });

    await user.type(screen.getByLabelText('Correo electrónico'), '  Admin@Demo.TEST ');
    await user.type(screen.getByLabelText('Contraseña'), ' demo-admin-pass');
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    const loginCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST');
    expect(JSON.parse(String(loginCall?.[1]?.body))).toEqual({
      email: 'admin@demo.test',
      password: ' demo-admin-pass',
      tenant_slug: 'demo',
    });
    expect(screen.getByTestId('location')).toHaveTextContent('/chat');
    expect(window.sessionStorage.length).toBe(1);
  });

  async function logInFrom(from: unknown) {
    installFakeApi({ [LOGIN]: loginOk(), [ME]: { status: 200, body: meBody('admin') } });
    const { user } = renderApp({ route: '/login', state: { from } });
    await user.type(screen.getByLabelText('Correo electrónico'), 'admin@demo.test');
    await user.type(screen.getByLabelText('Contraseña'), 'demo-admin-pass');
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));
  }

  it('returns to the protected page that sent you to the login', async () => {
    await logInFrom('/conocimiento');

    expect(await screen.findByRole('heading', { level: 1, name: 'Base de conocimiento' })).toBeInTheDocument();
    expect(screen.getByTestId('location').textContent).toBe('/conocimiento');
  });

  it.each(['/\\evil.example', '/\\/evil.example', '//evil.example', 'https://evil.example', '/login'])(
    'never returns to %s: it opens the chat instead',
    async (from) => {
      await logInFrom(from);

      expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
      expect(screen.getByTestId('location').textContent).toBe('/chat');
    },
  );

  it('announces invalid credentials and stays on the login page', async () => {
    installFakeApi({ [LOGIN]: { status: 401, body: { detail: 'invalid credentials' } } });
    const { user } = renderApp({ route: '/login' });

    await user.type(screen.getByLabelText('Correo electrónico'), 'admin@demo.test');
    await user.type(screen.getByLabelText('Contraseña'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'La empresa, el correo electrónico o la contraseña no son correctos.',
    );
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    expect(screen.queryByText(/Tu sesión ha caducado/)).not.toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
  });

  it('announces when the server cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const { user } = renderApp({ route: '/login' });

    await user.type(screen.getByLabelText('Correo electrónico'), 'admin@demo.test');
    await user.type(screen.getByLabelText('Contraseña'), 'demo-admin-pass');
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/No se ha podido conectar con el servidor/);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Iniciar sesión' })).toBeEnabled());
  });
});
