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
  it.each<Role>(['viewer', 'operator'])('hides Team from a %s', async (role) => {
    signedIn(role, '/chat');
    await screen.findByRole('heading', { level: 1, name: 'Chat' });

    const nav = mainNav();
    expect(within(nav).getByRole('link', { name: 'Chat' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('link', { name: 'Base de conocimiento' })).toBeInTheDocument();
    expect(within(nav).queryByRole('link', { name: 'Equipo' })).not.toBeInTheDocument();
    expect(within(nav).queryByText('Configuración')).not.toBeInTheDocument();
  });

  it.each<Role>(['viewer', 'operator'])('redirects a %s away from the Team route', async (role) => {
    signedIn(role, '/configuracion/equipo');

    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/chat');
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
