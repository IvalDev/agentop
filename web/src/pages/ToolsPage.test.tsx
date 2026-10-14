import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AXE_TEST_TIMEOUT_MS, expectNoAxeViolations } from '../test/axe';
import { createGate, installFakeApi, routesCalled, signedInRoutes, type FakeReply, type FakeRoutes } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

const ME = 'GET /api/v1/auth/me';
const LIST = 'GET /api/v1/modules';
const ACTIVATE = 'POST /api/v1/modules/calendario/activate';
const DEACTIVATE = 'DELETE /api/v1/modules/correo/activate';
const TABLE_NAME = 'Módulos disponibles para tu empresa';

interface ModuleBody {
  key: string;
  name: string;
  description: string;
  version: string;
  permissions: string[];
  tools: string[];
  active: boolean;
}

function moduleFixture(fields: Partial<ModuleBody> & Pick<ModuleBody, 'key' | 'name'>): ModuleBody {
  return {
    description: 'Descripción de prueba.',
    version: '1.0.0',
    permissions: [],
    tools: [],
    active: false,
    ...fields,
  };
}

/** The production catalog is empty; these two entries exercise the list and both states. */
function catalog(): ModuleBody[] {
  return [
    moduleFixture({
      key: 'calendario',
      name: 'Calendario',
      description: 'Gestiona citas y eventos.',
      version: '1.2.0',
      permissions: ['calendar:read'],
      tools: ['calendar.list_events'],
      active: false,
    }),
    moduleFixture({ key: 'correo', name: 'Correo', description: 'Lee y envía mensajes.', version: '0.3.1', active: true }),
  ];
}

function listReply(modules: ModuleBody[]): FakeReply {
  return { status: 200, body: { modules } };
}

async function openTools(routes: FakeRoutes = {}, caller: 'owner' | 'admin' = 'admin') {
  const fetchMock = installFakeApi({ ...signedInRoutes(caller), [LIST]: listReply(catalog()), ...routes });
  const view = renderApp({ route: '/configuracion/herramientas', storedToken: `${caller}-token` });
  await screen.findByRole('table', { name: TABLE_NAME });
  return { ...view, fetchMock };
}

function rowOf(name: string): HTMLElement {
  return screen.getByRole('rowheader', { name }).closest('tr')!;
}

/** Description, version, state, actions (the module name is the row header). */
function cellsOf(name: string): HTMLElement[] {
  return within(rowOf(name)).getAllByRole('cell');
}

function stateOf(name: string): HTMLElement {
  return cellsOf(name)[2]!;
}

describe('ToolsPage: the module list', () => {
  it('lists the catalog with state and offers activate or deactivate, with no axe violations', { timeout: AXE_TEST_TIMEOUT_MS }, async () => {
    await openTools();

    const table = screen.getByRole('table', { name: TABLE_NAME });
    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Módulo',
      'Descripción',
      'Versión',
      'Estado',
      'Acciones',
    ]);

    expect(rowOf('Calendario')).toHaveTextContent('Gestiona citas y eventos.');
    expect(rowOf('Calendario')).toHaveTextContent('1.2.0');
    expect(rowOf('Calendario')).toHaveTextContent('Herramientas: calendar.list_events');
    expect(rowOf('Calendario')).toHaveTextContent('Permisos: calendar:read');
    expect(stateOf('Calendario')).toHaveTextContent(/Inactivo$/);
    expect(within(rowOf('Calendario')).getByRole('button', { name: 'Activar «Calendario»' })).toBeInTheDocument();

    expect(stateOf('Correo')).toHaveTextContent(/Activo$/);
    expect(within(rowOf('Correo')).getByRole('button', { name: 'Desactivar «Correo»' })).toBeInTheDocument();

    await expectNoAxeViolations();
  });

  it('reports a failed load in Spanish', async () => {
    installFakeApi({ ...signedInRoutes('admin'), [LIST]: { status: 500, body: { detail: 'internal error' } } });
    renderApp({ route: '/configuracion/herramientas', storedToken: 'admin-token' });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('No se han podido cargar los módulos.');
    expect(alert).not.toHaveTextContent('internal error');
  });

  it('shows an empty state when the catalog has no modules', async () => {
    installFakeApi({ ...signedInRoutes('owner'), [LIST]: listReply([]) });
    renderApp({ route: '/configuracion/herramientas', storedToken: 'owner-token' });

    expect(await screen.findByText('Todavía no hay módulos disponibles.')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('ToolsPage: activating and deactivating', () => {
  it('activates a module after confirming, and Cancelar sends nothing', async () => {
    const { user, fetchMock } = await openTools({
      [ACTIVATE]: { status: 200, body: { key: 'calendario', active: true } },
    });

    await user.click(screen.getByRole('button', { name: 'Activar «Calendario»' }));
    const panel = screen.getByRole('region', { name: 'Activar Calendario' });
    expect(within(panel).getByRole('heading', { name: 'Activar Calendario' })).toHaveFocus();
    expect(routesCalled(fetchMock)).toEqual([ME, LIST]);

    await user.click(within(panel).getByRole('button', { name: 'Cancelar' }));
    expect(screen.queryByRole('region', { name: /Activar Calendario/ })).not.toBeInTheDocument();
    expect(routesCalled(fetchMock)).toEqual([ME, LIST]);

    await user.click(screen.getByRole('button', { name: 'Activar «Calendario»' }));
    await user.click(
      within(screen.getByRole('region', { name: 'Activar Calendario' })).getByRole('button', { name: 'Activar' }),
    );

    expect(await screen.findByText('Has activado el módulo Calendario.')).toBeInTheDocument();
    expect(stateOf('Calendario')).toHaveTextContent(/Activo$/);
    expect(routesCalled(fetchMock)).toEqual([ME, LIST, ACTIVATE]);
    expect(screen.getByRole('button', { name: 'Desactivar «Calendario»' })).toHaveFocus();
  });

  it('deactivates a module only after confirming', async () => {
    const { user, fetchMock } = await openTools({
      [DEACTIVATE]: { status: 200, body: { key: 'correo', active: false } },
    });

    await user.click(within(rowOf('Correo')).getByRole('button', { name: 'Desactivar «Correo»' }));
    const panel = screen.getByRole('region', { name: 'Desactivar Correo' });
    expect(routesCalled(fetchMock)).toEqual([ME, LIST]);

    await user.click(within(panel).getByRole('button', { name: 'Desactivar' }));

    expect(await screen.findByText('Has desactivado el módulo Correo.')).toBeInTheDocument();
    expect(stateOf('Correo')).toHaveTextContent(/Inactivo$/);
    expect(routesCalled(fetchMock)).toEqual([ME, LIST, DEACTIVATE]);
  });

  it('disables the confirmation while a change is running', async () => {
    const gate = createGate();
    const { user } = await openTools({
      [ACTIVATE]: () => gate.hold(() => ({ status: 200, body: { key: 'calendario', active: true } })),
    });

    await user.click(screen.getByRole('button', { name: 'Activar «Calendario»' }));
    const panel = screen.getByRole('region', { name: 'Activar Calendario' });
    const confirm = within(panel).getByRole('button', { name: 'Activar' });
    await user.click(confirm);
    await waitFor(() => expect(gate.pending).toBe(1));

    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(within(rowOf('Calendario')).getByRole('button', { name: 'Activar «Calendario»' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );

    gate.release();
    expect(await screen.findByText('Has activado el módulo Calendario.')).toBeInTheDocument();
  });

  it('explains a refused change (404) in Spanish and keeps the module inactive', async () => {
    const { user } = await openTools({
      [ACTIVATE]: { status: 404, body: { detail: 'module not found' } },
    });

    await user.click(screen.getByRole('button', { name: 'Activar «Calendario»' }));
    await user.click(
      within(screen.getByRole('region', { name: 'Activar Calendario' })).getByRole('button', { name: 'Activar' }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Ese módulo ya no está en el catálogo.');
    expect(alert).not.toHaveTextContent('module not found');
    expect(stateOf('Calendario')).toHaveTextContent(/Inactivo$/);
  });
});
