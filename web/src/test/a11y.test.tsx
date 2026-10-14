import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { expectNoAxeViolations } from './axe';
import { installFakeApi, signedInRoutes } from './fakeApi';
import { renderApp } from './renderApp';

describe('accessibility baseline (axe)', () => {
  it('login page has no violations, including after a failed submit', async () => {
    installFakeApi({});
    const { user } = renderApp({ route: '/login' });
    await expectNoAxeViolations();

    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }));
    await expectNoAxeViolations();
  });

  it('dashboard shell has landmarks, a skip link, and no violations', async () => {
    installFakeApi(signedInRoutes('owner'));
    const { user } = renderApp({ route: '/chat', storedToken: 'owner-token' });
    await screen.findByRole('heading', { level: 1, name: 'Chat' });

    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Principal' })).toBeInTheDocument();
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
    expect(screen.getByRole('link', { name: 'Saltar al contenido principal' })).toHaveAttribute(
      'href',
      '#main-content',
    );
    await expectNoAxeViolations();

    await user.click(screen.getByRole('button', { name: 'Menú' }));
    await expectNoAxeViolations();
  });
});
