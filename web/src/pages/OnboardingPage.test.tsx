import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { oauthAuthorizePath } from '../api/onboarding';
import { AXE_TEST_TIMEOUT_MS, expectNoAxeViolations } from '../test/axe';
import { installFakeApi, routesCalled, signedInRoutes, type FakeReply, type FakeRoutes } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

const ME = 'GET /api/v1/auth/me';
const STATE = 'GET /api/v1/onboarding/calendar';
const SUBMIT_TEST = 'POST /api/v1/onboarding/calendar/steps/test_connection';
const SUBMIT_FORM = 'POST /api/v1/onboarding/calendar/steps/credentials';
const SUBMIT_SCOPES = 'POST /api/v1/onboarding/calendar/steps/pick_scopes';
const RESTART = 'POST /api/v1/onboarding/calendar/restart';
const ROUTE = '/configuracion/herramientas/calendar/onboarding';

interface FieldBody {
  key: string;
  label: string;
  type: string;
  required: boolean;
  secret: boolean;
  help_text?: string;
  options?: string[];
}

interface StepBody {
  key: string;
  type: string;
  title: string;
  description: string;
  fields: FieldBody[];
}

function stepFixture(fields: Partial<StepBody> & Pick<StepBody, 'key' | 'type'>): StepBody {
  const { fields: rawFields, ...rest } = fields;
  return {
    title: 'Paso de prueba',
    description: 'Descripción del paso de prueba.',
    ...rest,
    // The API always serialises the field defaults, so the fixtures do too.
    fields: (rawFields ?? []).map((field) => ({ help_text: '', options: [], ...field })),
  };
}

function stateFixture(fields: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    module_key: 'calendar',
    status: 'in_progress',
    current_step: 'connect_google',
    step: stepFixture({ key: 'connect_google', type: 'redirect' }),
    data: {},
    created_at: null,
    updated_at: null,
    ...fields,
  };
}

function stateReply(body: Record<string, unknown>): FakeReply {
  return { status: 200, body };
}

const COMPLETED = stateFixture({ status: 'completed', current_step: null, step: null });
const REDIRECT = stateFixture({});
const TEST_STEP = stateFixture({
  current_step: 'test_connection',
  step: stepFixture({
    key: 'test_connection',
    type: 'test_connection',
    title: 'Verificar la conexión',
    description: 'Comprueba el acceso a la cuenta conectada.',
  }),
});

async function openWizard(routes: FakeRoutes = {}, role: 'admin' | 'viewer' = 'viewer') {
  const fetchMock = installFakeApi({ ...signedInRoutes(role), [STATE]: stateReply(REDIRECT), ...routes });
  const view = renderApp({ route: ROUTE, storedToken: `${role}-token` });
  await screen.findByRole('heading', { level: 1, name: 'Configurar la herramienta' });
  await waitFor(() => expect(screen.queryByText('Cargando la configuración…')).not.toBeInTheDocument());
  return { ...view, fetchMock };
}

/** `location.assign` cannot be spied on jsdom's native object; the test replaces the global. */
function stubLocation() {
  const assign = vi.fn();
  vi.stubGlobal('location', { assign, href: 'http://localhost/' });
  return assign;
}

/** The JSON body of the single POST the page sent, or undefined when it sent none. */
function postedBody(fetchMock: ReturnType<typeof installFakeApi>): unknown {
  const call = fetchMock.mock.calls.find(([, init]) => (init?.method ?? 'GET').toUpperCase() === 'POST');
  return call === undefined ? undefined : JSON.parse(String(call[1]?.body));
}

describe('OnboardingPage: the generic step renderer', () => {
  it('renders a redirect step with the server copy and connects through the OAuth start URL', async () => {
    const assign = stubLocation();
    const { user, fetchMock } = await openWizard({
      [STATE]: stateReply(
        stateFixture({
          step: stepFixture({
            key: 'connect_google',
            type: 'redirect',
            title: 'Conéctate con Google',
            description: 'Autoriza el acceso a tu cuenta de Google.',
          }),
        }),
      ),
    });

    expect(screen.getByRole('heading', { level: 1, name: 'Configurar la herramienta' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Conéctate con Google' })).toBeInTheDocument();
    expect(screen.getByText('Autoriza el acceso a tu cuenta de Google.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Conectar' }));

    expect(oauthAuthorizePath('calendar')).toBe('/api/v1/oauth/calendar/authorize');
    expect(assign).toHaveBeenCalledWith('/api/v1/oauth/calendar/authorize');
    // A redirect step is never POSTed: the browser leaves for the provider.
    expect(routesCalled(fetchMock)).toEqual([ME, STATE]);
  });

  it('runs the connection test without answers and advances to the completed view', { timeout: AXE_TEST_TIMEOUT_MS }, async () => {
    const { user, fetchMock } = await openWizard({
      [STATE]: stateReply(TEST_STEP),
      [SUBMIT_TEST]: stateReply(COMPLETED),
    });

    expect(screen.getByRole('heading', { level: 2, name: 'Verificar la conexión' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Probar conexión' }));

    expect(await screen.findByRole('heading', { level: 2, name: 'Configuración completada' })).toBeInTheDocument();
    expect(postedBody(fetchMock)).toEqual({ answers: {} });

    await expectNoAxeViolations();
  });

  it('explains a refused connection test (409) with a retry, in Spanish', async () => {
    const { user } = await openWizard({
      [STATE]: stateReply(TEST_STEP),
      [SUBMIT_TEST]: { status: 409, body: { detail: 'connection test failed' } },
    });

    await user.click(screen.getByRole('button', { name: 'Probar conexión' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'La conexión con el proveedor ha fallado. Comprueba la cuenta conectada e inténtalo de nuevo.',
    );
    expect(alert).not.toHaveTextContent('connection test failed');
    expect(screen.getByRole('button', { name: 'Probar conexión' })).toBeEnabled();
  });

  it('renders a form step, masks secret fields, and submits the answers', async () => {
    const formStep = stepFixture({
      key: 'credentials',
      type: 'form',
      title: 'Datos de la cuenta',
      description: 'Introduce las credenciales del servicio.',
      fields: [
        {
          key: 'client_id',
          label: 'Identificador',
          type: 'text',
          required: true,
          secret: false,
          help_text: 'El ID de la aplicación.',
        },
        { key: 'client_secret', label: 'Secreto', type: 'text', required: true, secret: true },
        { key: 'region', label: 'Región', type: 'select', required: false, secret: false, options: ['eu', 'us'] },
      ],
    });
    const { user, fetchMock } = await openWizard({
      [STATE]: stateReply(stateFixture({ current_step: 'credentials', step: formStep })),
      [SUBMIT_FORM]: stateReply(COMPLETED),
    });

    expect(screen.getByLabelText('Secreto')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText('Identificador')).toHaveAttribute('type', 'text');
    expect(screen.getByRole('combobox', { name: 'Región' })).toBeInTheDocument();
    expect(screen.getByText('El ID de la aplicación.')).toBeInTheDocument();

    await user.type(screen.getByLabelText('Identificador'), 'app-1');
    await user.type(screen.getByLabelText('Secreto'), 's3cr3t');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Región' }), 'us');
    await user.click(screen.getByRole('button', { name: 'Guardar y continuar' }));

    await screen.findByRole('heading', { level: 2, name: 'Configuración completada' });
    expect(postedBody(fetchMock)).toEqual({
      answers: { client_id: 'app-1', client_secret: 's3cr3t', region: 'us' },
    });
  });

  it('blocks required form fields locally and maps a 422 onto the offending field', async () => {
    const formStep = stepFixture({
      key: 'credentials',
      type: 'form',
      fields: [
        { key: 'client_id', label: 'Identificador', type: 'text', required: true, secret: false },
        { key: 'client_secret', label: 'Secreto', type: 'text', required: true, secret: true },
      ],
    });
    const { user, fetchMock } = await openWizard({
      [STATE]: stateReply(stateFixture({ current_step: 'credentials', step: formStep })),
      [SUBMIT_FORM]: { status: 422, body: { detail: "missing required answer 'client_secret'" } },
    });

    // Client-side: nothing reaches the server without the required values.
    await user.click(screen.getByRole('button', { name: 'Guardar y continuar' }));
    expect(screen.getAllByText('Indica este campo.')).toHaveLength(2);
    expect(routesCalled(fetchMock)).toEqual([ME, STATE]);

    await user.type(screen.getByLabelText('Identificador'), 'app-1');
    await user.type(screen.getByLabelText('Secreto'), '   ');
    await user.click(screen.getByRole('button', { name: 'Guardar y continuar' }));
    expect(routesCalled(fetchMock)).toEqual([ME, STATE]);

    // Server-side 422: the named key gets the inline error, the detail stays private.
    await user.clear(screen.getByLabelText('Secreto'));
    await user.type(screen.getByLabelText('Secreto'), 's3cr3t');
    await user.click(screen.getByRole('button', { name: 'Guardar y continuar' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Los datos no son válidos. Revisa los campos e inténtalo de nuevo.');
    expect(alert).not.toHaveTextContent('client_secret');
    await waitFor(() => expect(screen.getByLabelText('Secreto')).toHaveAttribute('aria-invalid', 'true'));
    expect(screen.getByLabelText('Identificador')).not.toHaveAttribute('aria-invalid');
    expect(screen.getByText('Revisa este campo.')).toBeInTheDocument();
  });

  it('renders a scope_select step as a checkbox list and submits the chosen scopes', async () => {
    const scopeStep = stepFixture({
      key: 'pick_scopes',
      type: 'scope_select',
      title: 'Permisos',
      description: 'Elige los permisos que autorizas.',
      fields: [
        {
          key: 'scopes',
          label: 'Scopes',
          type: 'select',
          required: true,
          secret: false,
          options: ['calendar.events', 'calendar.readonly'],
        },
      ],
    });
    const { user, fetchMock } = await openWizard({
      [STATE]: stateReply(stateFixture({ current_step: 'pick_scopes', step: scopeStep })),
      [SUBMIT_SCOPES]: stateReply(COMPLETED),
    });

    const boxes = screen.getAllByRole('checkbox');
    expect(boxes).toHaveLength(2);
    await user.click(boxes[0]!);
    await user.click(boxes[1]!);
    await user.click(screen.getByRole('button', { name: 'Guardar y continuar' }));

    await screen.findByRole('heading', { level: 2, name: 'Configuración completada' });
    expect(postedBody(fetchMock)).toEqual({
      answers: { scopes: ['calendar.events', 'calendar.readonly'] },
    });
  });

  it('offers a confirmed restart from the completed view and lands on the first step again', async () => {
    const { user, fetchMock } = await openWizard({
      [STATE]: stateReply(COMPLETED),
      [RESTART]: stateReply(REDIRECT),
    });

    expect(screen.getByRole('heading', { level: 2, name: 'Configuración completada' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Reconectar' }));
    const panel = screen.getByRole('region', { name: '¿Reconectar la herramienta?' });
    expect(routesCalled(fetchMock)).toEqual([ME, STATE]);

    await user.click(within(panel).getByRole('button', { name: 'Reconectar' }));

    expect(await screen.findByRole('heading', { level: 2, name: 'Paso de prueba' })).toBeInTheDocument();
    expect(routesCalled(fetchMock)).toEqual([ME, STATE, RESTART]);
  });

  it('explains an inactive module (409) and links back to Mis herramientas', async () => {
    await openWizard({ [STATE]: { status: 409, body: { detail: 'module is not active for this tenant' } } });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'Esta herramienta no está activada para tu empresa. Pide a tu administrador que la active.',
    );
    expect(alert).not.toHaveTextContent('not active');
    expect(screen.getByRole('link', { name: 'Volver a Mis herramientas' })).toHaveAttribute(
      'href',
      '/configuracion/mis-herramientas',
    );
  });

  it('explains an unknown module (404) and links back to Mis herramientas', async () => {
    await openWizard({ [STATE]: { status: 404, body: { detail: 'module not found' } } });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('No encontramos esa herramienta. Puede que ya no esté disponible.');
    expect(screen.getByRole('link', { name: 'Volver a Mis herramientas' })).toHaveAttribute(
      'href',
      '/configuracion/mis-herramientas',
    );
  });

  it('lets a viewer reach the wizard (the OAuth landing must not bounce a non-admin)', async () => {
    await openWizard({}, 'viewer');

    expect(screen.getByRole('heading', { level: 1, name: 'Configurar la herramienta' })).toBeInTheDocument();
  });
});
