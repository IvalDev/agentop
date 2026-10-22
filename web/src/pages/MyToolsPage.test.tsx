import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { formatDate } from '../i18n';
import { AXE_TEST_TIMEOUT_MS, expectNoAxeViolations } from '../test/axe';
import {
  createGate,
  installFakeApi,
  routesCalled,
  signedInRoutes,
  type FakeReply,
  type FakeRoutes,
} from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

const LIST = 'GET /api/v1/modules';
const CONNECTORS = 'GET /api/v1/connectors';
const PAIRING_CODE = 'POST /api/v1/connectors/pairing-codes';
const ONBOARDING_CALENDAR = 'GET /api/v1/onboarding/calendar';
const ONBOARDING_CORREO = 'GET /api/v1/onboarding/correo';
const ROUTE = '/configuracion/mis-herramientas';
const TABLE_NAME = 'Herramientas activas de tu empresa';
const WORKSPACE_TABLE_NAME = 'Equipos conectados a tu espacio de trabajo';

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

interface ConnectorBody {
  id: string;
  name: string;
  connected: boolean;
  last_seen_at: string | null;
  created_at: string;
}

const LAPTOP: ConnectorBody = {
  id: 'conector-1',
  name: 'MacBook de Vera',
  connected: true,
  last_seen_at: '2026-10-20T12:00:00Z',
  created_at: '2026-10-19T09:00:00Z',
};

const OFFICE_PC: ConnectorBody = {
  id: 'conector-2',
  name: 'PC de la oficina',
  connected: false,
  last_seen_at: null,
  created_at: '2026-10-18T09:00:00Z',
};

function connectorsReply(connectors: ConnectorBody[]): FakeReply {
  return { status: 200, body: { connectors } };
}

async function openWithWorkspace(
  connectors: ConnectorBody[] = [],
  routes: FakeRoutes = {},
  role: 'owner' | 'admin' | 'viewer' = 'viewer',
) {
  return openMyTools({ [CONNECTORS]: connectorsReply(connectors), ...routes }, role);
}

async function openMyTools(routes: FakeRoutes = {}, role: 'owner' | 'admin' | 'viewer' = 'viewer') {
  const fetchMock = installFakeApi({
    ...signedInRoutes(role),
    [LIST]: listReply(catalog()),
    [CONNECTORS]: connectorsReply([]),
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
      [CONNECTORS]: connectorsReply([]),
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
    installFakeApi({
      ...signedInRoutes('viewer'),
      [LIST]: { status: 500, body: { detail: 'internal error' } },
      [CONNECTORS]: connectorsReply([]),
    });
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

describe('MyToolsPage: local workspace card', () => {
  it(
    'lists the devices with their connection status and last connection, and no axe violations',
    { timeout: AXE_TEST_TIMEOUT_MS },
    async () => {
      await openWithWorkspace([LAPTOP, OFFICE_PC]);

      const table = await screen.findByRole('table', { name: WORKSPACE_TABLE_NAME });
      expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
        'Equipo',
        'Estado',
        'Última conexión',
        'Acciones',
      ]);

      const laptop = rowOf('MacBook de Vera');
      expect(laptop).toHaveTextContent('Conectado');
      expect(laptop).toHaveTextContent(
        formatDate(LAPTOP.last_seen_at!, { dateStyle: 'medium', timeStyle: 'short' }),
      );
      expect(within(laptop).getByRole('button', { name: 'Revocar «MacBook de Vera»' })).toBeInTheDocument();

      const officePc = rowOf('PC de la oficina');
      expect(officePc).toHaveTextContent('Desconectado');
      expect(officePc).toHaveTextContent('Nunca se ha conectado');

      // The card manages the permission and the connection: never a folder path or its files.
      expect(screen.queryByText(/\/(Users|home)\//)).not.toBeInTheDocument();

      await expectNoAxeViolations();
    },
  );

  it('explains how to connect the first device', async () => {
    await openWithWorkspace([]);

    expect(
      await screen.findByText(
        'Todavía no has conectado ningún equipo. Genera un código y pégalo en el widget.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: WORKSPACE_TABLE_NAME })).not.toBeInTheDocument();
  });

  it('generates a pairing code with its expiry and where to paste it', async () => {
    const gate = createGate();
    const expiresAt = '2026-10-20T12:10:00Z';
    const { user, fetchMock } = await openWithWorkspace([], {
      [PAIRING_CODE]: () =>
        gate.hold(() => ({ status: 201, body: { code: 'ABCD-EFGH', expires_at: expiresAt } })),
    });

    await user.click(screen.getByRole('button', { name: 'Generar código' }));

    expect(screen.getByRole('button', { name: 'Generando código…' })).toHaveAttribute('aria-disabled', 'true');
    expect(gate.pending).toBe(1);

    gate.release();

    expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument();
    expect(
      screen.getByText(
        `El código caduca el ${formatDate(expiresAt, { dateStyle: 'medium', timeStyle: 'short' })}.`,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Abre el widget, entra en el menú → Espacio de trabajo y pega el código.'),
    ).toBeInTheDocument();
    expect(routesCalled(fetchMock)).toContain(PAIRING_CODE);
  });

  it('revokes a device only after the confirmation and removes it from the list', async () => {
    const deleteRoute = 'DELETE /api/v1/connectors/conector-1';
    const { user, fetchMock } = await openWithWorkspace([LAPTOP], { [deleteRoute]: { status: 204 } });

    await screen.findByRole('rowheader', { name: 'MacBook de Vera' });
    await user.click(screen.getByRole('button', { name: 'Revocar «MacBook de Vera»' }));

    const panel = screen.getByRole('region', { name: 'Revocar «MacBook de Vera»' });
    expect(panel).toHaveTextContent('El equipo dejará de estar conectado');
    expect(routesCalled(fetchMock)).not.toContain(deleteRoute);

    await user.click(within(panel).getByRole('button', { name: 'Revocar' }));

    await waitFor(() =>
      expect(screen.queryByRole('rowheader', { name: 'MacBook de Vera' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('status')).toHaveTextContent('Has revocado el acceso de «MacBook de Vera».');
    expect(routesCalled(fetchMock)).toContain(deleteRoute);
  });

  it('keeps the device and reports a failed revoke in Spanish', async () => {
    const { user } = await openWithWorkspace([LAPTOP], {
      'DELETE /api/v1/connectors/conector-1': { status: 500, body: { detail: 'boom' } },
    });

    await screen.findByRole('rowheader', { name: 'MacBook de Vera' });
    await user.click(screen.getByRole('button', { name: 'Revocar «MacBook de Vera»' }));
    await user.click(
      within(screen.getByRole('region', { name: 'Revocar «MacBook de Vera»' })).getByRole('button', {
        name: 'Revocar',
      }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'No se ha podido revocar el equipo. Comprueba la conexión e inténtalo de nuevo.',
    );
    expect(alert).not.toHaveTextContent('boom');
    expect(screen.getByRole('rowheader', { name: 'MacBook de Vera' })).toBeInTheDocument();
  });

  it('shows the devices loading state until the reply arrives', async () => {
    const gate = createGate();
    await openWithWorkspace([], {
      [CONNECTORS]: () => gate.hold(() => connectorsReply([LAPTOP])),
    });

    expect(screen.getByText('Cargando tus equipos…')).toBeInTheDocument();

    gate.release();

    expect(await screen.findByRole('rowheader', { name: 'MacBook de Vera' })).toBeInTheDocument();
    expect(screen.queryByText('Cargando tus equipos…')).not.toBeInTheDocument();
  });

  it('reports a failed device load in Spanish and retries', async () => {
    let calls = 0;
    const { user } = await openWithWorkspace([], {
      [CONNECTORS]: () =>
        (calls += 1) === 1
          ? { status: 500, body: { detail: 'internal error' } }
          : connectorsReply([LAPTOP]),
    });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'No se han podido cargar tus equipos. Comprueba la conexión e inténtalo de nuevo.',
    );
    expect(alert).not.toHaveTextContent('internal error');

    await user.click(screen.getByRole('button', { name: 'Reintentar' }));

    expect(await screen.findByRole('rowheader', { name: 'MacBook de Vera' })).toBeInTheDocument();
  });

  it('reports a failed code request in Spanish, never the server text', async () => {
    const { user } = await openWithWorkspace([], {
      [PAIRING_CODE]: { status: 500, body: { detail: 'boom' } },
    });

    await user.click(screen.getByRole('button', { name: 'Generar código' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'No se ha podido generar el código. Comprueba la conexión e inténtalo de nuevo.',
    );
    expect(alert).not.toHaveTextContent('boom');
  });
});
