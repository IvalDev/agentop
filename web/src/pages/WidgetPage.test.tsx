import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AXE_TEST_TIMEOUT_MS, expectNoAxeViolations } from '../test/axe';
import { installFakeApi, meBody } from '../test/fakeApi';
import { doneFrame, installChatApi } from '../test/fakeChat';
import { renderApp } from '../test/renderApp';

const LOGIN = 'POST /api/v1/auth/login';
const ME = 'GET /api/v1/auth/me';

function loginOk() {
  return {
    status: 200,
    body: { access_token: 'fresh-token', token_type: 'bearer', expires_in: 1800 },
  };
}

describe('WidgetPage', () => {
  it('sends an anonymous visitor to the login and returns to /widget after signing in', async () => {
    installFakeApi({ [LOGIN]: loginOk(), [ME]: { status: 200, body: meBody('viewer') } });
    const { user } = renderApp({ route: '/widget' });

    expect(await screen.findByRole('heading', { level: 1, name: 'Iniciar sesión' })).toBeInTheDocument();
    expect(screen.getByTestId('location').textContent).toBe('/login');

    await user.type(screen.getByLabelText('Correo electrónico'), 'viewer@demo.test');
    await user.type(screen.getByLabelText('Contraseña'), 'demo-viewer-pass');
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(screen.getByTestId('location').textContent).toBe('/widget');
    expect(screen.getByLabelText('Mensaje')).toBeInTheDocument();
  });

  it('shows the chat with the minimal header and none of the console chrome', async () => {
    installChatApi('viewer', [doneFrame('Hola.')]);
    renderApp({ route: '/widget', storedToken: 'viewer-token' });

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(document.title).toBe('Chat · Asistente de trabajo');
    expect(screen.getByTestId('location').textContent).toBe('/widget');

    // The shell the widget loads: own header, chat below, nothing else.
    const banner = screen.getByRole('banner');
    expect(within(banner).getByText('Asistente de trabajo')).toBeInTheDocument();
    expect(within(banner).getByRole('button', { name: 'Cerrar sesión' })).toBeInTheDocument();
    expect(within(banner).queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Mensaje')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enviar' })).toBeInTheDocument();

    // No dashboard navigation, identity, or console links.
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.queryByText('Vera Consulta')).not.toBeInTheDocument();
    for (const link of ['Chat', 'Base de conocimiento', 'Ir al chat']) {
      expect(screen.queryByRole('link', { name: link })).not.toBeInTheDocument();
    }
  });

  it('streams an answer through the widget chat, like the dashboard', async () => {
    const { requests } = installChatApi('viewer', [doneFrame('Hola desde el widget.')]);
    const { user } = renderApp({ route: '/widget', storedToken: 'viewer-token' });
    const input = await screen.findByLabelText('Mensaje');

    await user.type(input, 'Hola{Enter}');

    const log = screen.getByRole('log', { name: 'Conversación' });
    expect(await within(log).findByText('Hola desde el widget.')).toBeInTheDocument();
    expect(requests[0]!.message).toBe('Hola');
  });

  it('closes the session from the minimal header and returns to the login', async () => {
    installChatApi('viewer', []);
    const { user } = renderApp({ route: '/widget', storedToken: 'viewer-token' });
    await screen.findByRole('heading', { level: 1, name: 'Chat' });

    await user.click(screen.getByRole('button', { name: 'Cerrar sesión' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Iniciar sesión' })).toBeInTheDocument();
    expect(screen.getByTestId('location').textContent).toBe('/login');
    expect(screen.getByRole('status')).toHaveTextContent('Has cerrado la sesión.');
    expect(window.sessionStorage.length).toBe(0);
  });

  it('has no axe violations, empty and with an answer', { timeout: AXE_TEST_TIMEOUT_MS }, async () => {
    installChatApi('viewer', [doneFrame('Hola.')]);
    const { user } = renderApp({ route: '/widget', storedToken: 'viewer-token' });
    const input = await screen.findByLabelText('Mensaje');
    await expectNoAxeViolations();

    await user.type(input, 'Hola{Enter}');
    await within(screen.getByRole('log', { name: 'Conversación' })).findByText('Hola.');

    await expectNoAxeViolations();
  });
});
