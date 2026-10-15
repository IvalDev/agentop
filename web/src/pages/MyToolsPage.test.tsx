import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AXE_TEST_TIMEOUT_MS, expectNoAxeViolations } from '../test/axe';
import { installFakeApi, signedInRoutes, type FakeReply, type FakeRoutes } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

const LIST = 'GET /api/v1/modules';
const ONBOARDING_CALENDAR = 'GET /api/v1/onboarding/calendar';
const ONBOARDING_CORREO = 'GET /api/v1/onboarding/correo';
const ROUTE = '/configuracion/mis-herramientas';
const TABLE_NAME = 'Herramientas activas de tu empresa';

interface ModuleBody {
  key: string;
  name: string;
  description: string;
  version: string;
  permissions: string[];
  tools: string[];
  active: boolean;
}

function moduleFixture(fields: Partial<ModuleBody> & Pick<ModuleBody, 'key' | 'name' | 'active'>): ModuleBody {
  return {
    description: 'Descripción de prueba.',
    version: '1.0.0',
    permissions: [],
    tools: [],
    ...fields,
  };
}

function catalog(): ModuleBody[] {
  return [
    moduleFixture({
      key: 'calendar',
      name: 'Calendario',
      description: 'Gestiona citas y eventos.',
      active: true,
    }),
    moduleFixture({ key: 'correo', name: 'Correo', description: 'Lee y envía mensajes.', active: true }),
    moduleFixture({ key: 'agenda', name: 'Agenda', description: 'Módulo sin activar.', active: false }),
  ];
}

function listReply(modules: ModuleBody[]): FakeReply {
  return { status: 200, body: { modules } };
}

function stateReply(moduleKey: string, status: 'not_started' | 'in_progress' | 'completed'): FakeReply {
  return {
    status: 200,
    body: {
      module_key: moduleKey,
      status,
      current_step: status === 'completed' ? null : 'connect_google',
      step: null,
      data: {},
      created_at: null,
      updated_at: null,
    },
  };
}

async function openMyTools(routes: FakeRoutes = {}, role: 'owner' | 'admin' | 'viewer' = 'viewer') {
  const fetchMock = installFakeApi({
    ...signedInRoutes(role),
    [LIST]: listReply(catalog()),
    [ONBOARDING_CALENDAR]: stateReply('calendar', 'completed'),
    [ONBOARDING_CORREO]: stateReply('correo', 'not_started'),
    ...routes,
  });
  const view = renderApp({ route: ROUTE, storedToken: `${role}-token` });
  await screen.findByRole('table', { name: TABLE_NAME });
  // The per-module status checks settle right after the catalog load.
  await waitFor(() => expect(screen.queryByText('Comprobando el estado…')).not.toBeInTheDocument());
  return { ...view, fetchMock };
}

function rowOf(name: string): HTMLElement {
  return screen.getByRole('rowheader', { name }).closest('tr')!;
}

describe('MyToolsPage: active modules and their status', () => {
  it('lists the active modules with connection status and no axe violations', { timeout: AXE_TEST_TIMEOUT_MS }, async () => {
    await openMyTools();

    const table = screen.getByRole('table', { name: TABLE_NAME });
    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Herramienta',
      'Descripción',
      'Estado',
      'Acciones',
    ]);

    expect(rowOf('Calendario')).toHaveTextContent('Gestiona citas y eventos.');
    expect(rowOf('Calendario')).toHaveTextContent('Conectado');
    expect(within(rowOf('Calendario')).queryByRole('link', { name: /Configurar/ })).not.toBeInTheDocument();

    expect(rowOf('Correo')).toHaveTextContent('Sin configurar');
    expect(within(rowOf('Correo')).getByRole('link', { name: 'Configurar «Correo»' })).toHaveAttribute(
      'href',
      '/configuracion/herramientas/correo/onboarding',
    );

    // Inactive modules are not the user's tools: they never appear here.
    expect(screen.queryByRole('rowheader', { name: 'Agenda' })).not.toBeInTheDocument();

    await expectNoAxeViolations();
  });

  it('shows an empty state when the tenant has no active module', async () => {
    installFakeApi({
      ...signedInRoutes('viewer'),
      [LIST]: listReply([moduleFixture({ key: 'agenda', name: 'Agenda', active: false })]),
    });
    renderApp({ route: ROUTE, storedToken: 'viewer-token' });

    expect(
      await screen.findByText(
        'Tu administrador aún no ha activado herramientas para tu empresa. Cuando active un módulo, aparecerá aquí para que puedas configurarlo.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reports a failed module load in Spanish, never the server text', async () => {
    installFakeApi({ ...signedInRoutes('viewer'), [LIST]: { status: 500, body: { detail: 'internal error' } } });
    renderApp({ route: ROUTE, storedToken: 'viewer-token' });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('No se han podido cargar tus herramientas. Comprueba la conexión e inténtalo de nuevo.');
    expect(alert).not.toHaveTextContent('internal error');
  });

  it('marks a module whose status could not be fetched', async () => {
    await openMyTools({ [ONBOARDING_CORREO]: { status: 500, body: { detail: 'boom' } } });

    expect(rowOf('Correo')).toHaveTextContent('No se ha podido comprobar el estado.');
    expect(rowOf('Correo')).not.toHaveTextContent('boom');
  });

  it('lets a viewer reach the page and navigate to the wizard', async () => {
    const { user } = await openMyTools({}, 'viewer');

    expect(screen.getByRole('heading', { level: 1, name: 'Mis herramientas' })).toBeInTheDocument();
    expect(document.title).toBe('Mis herramientas · Asistente de trabajo');

    await user.click(screen.getByRole('link', { name: 'Configurar «Correo»' }));

    expect(screen.getByTestId('location')).toHaveTextContent('/configuracion/herramientas/correo/onboarding');
  });
});
