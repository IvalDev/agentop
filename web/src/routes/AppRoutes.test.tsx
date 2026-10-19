import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Role } from '../session/roles';
import { installFakeApi, signedInRoutes } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

function signedIn(role: Role, route: string) {
  installFakeApi(signedInRoutes(role));
  return renderApp({ route, storedToken: `${role}-token` });
}

function mainNav() {
  return screen.getByRole('navigation', { name: 'Principal' });
}

describe('auth guard', () => {
  it('sends an anonymous visitor to the login page', async () => {
    installFakeApi({});
    renderApp({ route: '/conocimiento' });

    expect(await screen.findByRole('heading', { level: 1, name: 'Iniciar sesión' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
  });

  it('opens the chat at the root once logged in', async () => {
    signedIn('viewer', '/');

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/chat');
  });

  it('restores the session from this tab and shows who is signed in', async () => {
    signedIn('operator', '/conocimiento');

    expect(await screen.findByRole('heading', { level: 1, name: 'Base de conocimiento' })).toBeInTheDocument();
    const banner = screen.getByRole('banner');
    expect(within(banner).getByText('Óscar Operario')).toBeInTheDocument();
    expect(within(banner).getByText('Operador')).toBeInTheDocument();
  });

  it('skips the login page when already signed in', async () => {
    signedIn('viewer', '/login');

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
  });
});

describe('role-based navigation', () => {
  it.each<Role>(['viewer', 'operator'])('shows a %s only Mis herramientas inside Settings', async (role) => {
    signedIn(role, '/chat');
    await screen.findByRole('heading', { level: 1, name: 'Chat' });

    const nav = mainNav();
    expect(within(nav).getByRole('link', { name: 'Chat' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('link', { name: 'Base de conocimiento' })).toBeInTheDocument();
    expect(within(nav).getByText('Configuración')).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: 'Mis herramientas' })).toBeInTheDocument();
    expect(within(nav).queryByRole('link', { name: 'Equipo' })).not.toBeInTheDocument();
    expect(within(nav).queryByRole('link', { name: 'Herramientas' })).not.toBeInTheDocument();
  });

  it.each<Role>(['viewer', 'operator'])('redirects a %s away from the Team route', async (role) => {
    signedIn(role, '/configuracion/equipo');

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/chat');
  });

  it.each<Role>(['viewer', 'operator'])('redirects a %s away from the Tools route', async (role) => {
    signedIn(role, '/configuracion/herramientas');

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/chat');
  });

  it.each<Role>(['viewer', 'operator'])('lets a %s reach My tools (their own tools, not the admin panel)', async (role) => {
    signedIn(role, '/configuracion/mis-herramientas');

    expect(await screen.findByRole('heading', { level: 1, name: 'Mis herramientas' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/configuracion/mis-herramientas');
  });

  it.each<Role>(['viewer', 'operator'])('lets a %s reach the onboarding wizard (the OAuth landing)', async (role) => {
    installFakeApi({
      ...signedInRoutes(role),
      'GET /api/v1/onboarding/calendar': {
        status: 200,
        body: {
          module_key: 'calendar',
          status: 'not_started',
          current_step: 'connect_google',
          step: {
            key: 'connect_google',
            type: 'redirect',
            title: 'Conéctate con Google',
            description: 'Autoriza el acceso.',
            fields: [],
          },
          data: {},
          created_at: null,
          updated_at: null,
        },
      },
    });
    renderApp({ route: '/configuracion/herramientas/calendar/onboarding', storedToken: `${role}-token` });

    expect(await screen.findByRole('heading', { level: 1, name: 'Configurar la herramienta' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/configuracion/herramientas/calendar/onboarding');
  });

  it.each<Role>(['owner', 'admin'])('lets a %s reach Settings → Team', async (role) => {
    const { user } = signedIn(role, '/chat');
    await screen.findByRole('heading', { level: 1, name: 'Chat' });

    const nav = mainNav();
    expect(within(nav).getByText('Configuración')).toBeInTheDocument();
    await user.click(within(nav).getByRole('link', { name: 'Equipo' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Equipo' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/configuracion/equipo');
    expect(document.title).toBe('Equipo · Asistente de trabajo');
  });

  it.each<Role>(['owner', 'admin'])('lets a %s reach Settings → Tools', async (role) => {
    const { user } = signedIn(role, '/chat');
    await screen.findByRole('heading', { level: 1, name: 'Chat' });

    const nav = mainNav();
    expect(within(nav).getByText('Configuración')).toBeInTheDocument();
    await user.click(within(nav).getByRole('link', { name: 'Herramientas' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Herramientas' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/configuracion/herramientas');
    expect(document.title).toBe('Herramientas · Asistente de trabajo');
  });

  it('collapses the navigation behind a toggle that reports its state', async () => {
    const { user } = signedIn('admin', '/chat');
    await screen.findByRole('heading', { level: 1, name: 'Chat' });

    const toggle = within(mainNav()).getByRole('button', { name: 'Menú' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await user.keyboard('{Escape}');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveFocus();
  });

  it('shows a not-found page for unknown addresses inside the app', async () => {
    signedIn('viewer', '/no-existe');

    expect(await screen.findByRole('heading', { level: 1, name: 'Página no encontrada' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ir al chat' })).toHaveAttribute('href', '/chat');
  });
});
