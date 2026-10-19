import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Role } from '../session/roles';
import { AXE_TEST_TIMEOUT_MS, expectNoAxeViolations } from '../test/axe';
import { installFakeApi, routesCalled, signedInRoutes, type FakeReply, type FakeRoutes } from '../test/fakeApi';
import { renderApp } from '../test/renderApp';

const ME = 'GET /api/v1/auth/me';
const LIST = 'GET /api/v1/users';
const CREATE = 'POST /api/v1/users';
const TABLE_NAME = 'Personas de tu empresa';

interface MemberBody {
  id: number;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}

function member(fields: Partial<MemberBody> & Pick<MemberBody, 'id' | 'full_name' | 'role'>): MemberBody {
  return { email: `${fields.role}${fields.id}@demo.test`, is_active: true, created_at: '2026-10-14T12:00:00Z', ...fields };
}

/** The signed-in user is id 1 (`meBody`), with the caller's role and demo name. */
function teamFor(caller: 'owner' | 'admin'): MemberBody[] {
  return [
    caller === 'admin'
      ? member({ id: 1, full_name: 'Adrián Gestor', role: 'admin', email: 'admin@demo.test' })
      : member({ id: 1, full_name: 'Olga Dueña', role: 'owner', email: 'owner@demo.test' }),
    member({ id: 2, full_name: 'Paula Fundadora', role: 'owner', email: 'paula@demo.test' }),
    member({ id: 3, full_name: 'Óscar Operario', role: 'operator', email: 'operator@demo.test' }),
    member({
      id: 4,
      full_name: 'Vera Consulta',
      role: 'viewer',
      email: 'viewer@demo.test',
      is_active: false,
      created_at: '2026-10-10T12:00:00Z',
    }),
  ];
}

function listReply(users: MemberBody[]): FakeReply {
  return { status: 200, body: { users } };
}

async function openTeam(caller: 'owner' | 'admin', routes: FakeRoutes = {}) {
  const fetchMock = installFakeApi({ ...signedInRoutes(caller), [LIST]: listReply(teamFor(caller)), ...routes });
  const view = renderApp({ route: '/configuracion/equipo', storedToken: `${caller}-token` });
  await screen.findByRole('table', { name: TABLE_NAME });
  return { ...view, fetchMock };
}

function rowOf(name: string): HTMLElement {
  return screen.getByRole('rowheader', { name: new RegExp(`^${name}`) }).closest('tr')!;
}

/** Email, role, state, date added, actions. */
function cellsOf(name: string): HTMLElement[] {
  return within(rowOf(name)).getAllByRole('cell');
}

function bodyOf(fetchMock: ReturnType<typeof installFakeApi>, route: string): unknown {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => `${(init?.method ?? 'GET').toUpperCase()} ${String(input)}` === route,
  );
  return call?.[1]?.body === undefined ? undefined : JSON.parse(String(call[1].body));
}

function optionsOf(select: HTMLElement): string[] {
  return within(select).getAllByRole('option').map((option) => option.textContent ?? '');
}

describe('TeamPage: the member list', () => {
  it('lists every member with email, role, state, and date added, marking you', { timeout: AXE_TEST_TIMEOUT_MS }, async () => {
    await openTeam('admin');

    const table = screen.getByRole('table', { name: TABLE_NAME });
    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Nombre',
      'Correo electrónico',
      'Rol',
      'Estado',
      'Fecha de alta',
      'Acciones',
    ]);
    expect(screen.getByRole('rowheader', { name: 'Adrián Gestor (tú)' })).toBeInTheDocument();

    const [email, role, state, date] = cellsOf('Adrián Gestor');
    expect(email).toHaveTextContent(/^admin@demo\.test$/);
    expect(role).toHaveTextContent(/^Administrador$/);
    expect(state).toHaveTextContent(/Activo$/);
    expect(date).toHaveTextContent(/^14 oct 2026$/);

    expect(cellsOf('Paula Fundadora')[1]).toHaveTextContent(/^Propietario$/);
    expect(cellsOf('Óscar Operario')[1]).toHaveTextContent(/^Operador$/);
    const [, viewerRole, viewerState, viewerDate] = cellsOf('Vera Consulta');
    expect(viewerRole).toHaveTextContent(/^Lector$/);
    expect(viewerState).toHaveTextContent(/Inactivo$/);
    expect(viewerDate).toHaveTextContent(/^10 oct 2026$/);
    await expectNoAxeViolations();
  });

  it('reports a failed load in Spanish', async () => {
    installFakeApi({ ...signedInRoutes('admin'), [LIST]: { status: 500, body: { detail: 'internal error' } } });
    renderApp({ route: '/configuracion/equipo', storedToken: 'admin-token' });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('No se ha podido cargar el equipo.');
    expect(alert).not.toHaveTextContent('internal error');
  });

  it('disables the controls on your own row and, for an admin, on owners', async () => {
    await openTeam('admin');

    const own = rowOf('Adrián Gestor');
    expect(within(own).queryByRole('button')).not.toBeInTheDocument();
    expect(own).toHaveTextContent('No puedes modificar tu propia cuenta.');
    const owner = rowOf('Paula Fundadora');
    expect(within(owner).queryByRole('button')).not.toBeInTheDocument();
    expect(owner).toHaveTextContent('Solo un propietario puede modificar esta cuenta.');
    expect(within(rowOf('Óscar Operario')).getByRole('button', { name: 'Cambiar rol «Óscar Operario»' })).toBeInTheDocument();
    expect(within(rowOf('Óscar Operario')).getByRole('button', { name: 'Desactivar «Óscar Operario»' })).toBeInTheDocument();
  });

  it('lets an owner manage other owners, but not their own row', async () => {
    await openTeam('owner');

    expect(within(rowOf('Olga Dueña')).queryByRole('button')).not.toBeInTheDocument();
    expect(within(rowOf('Paula Fundadora')).getByRole('button', { name: 'Desactivar «Paula Fundadora»' })).toBeInTheDocument();
  });
});

describe('TeamPage: adding a member', () => {
  it('creates a member with a normalized email and shows them in the list', async () => {
    const { user, fetchMock } = await openTeam('admin', {
      [CREATE]: {
        status: 201,
        body: member({ id: 5, full_name: 'Ana López', role: 'operator', email: 'ana.lopez@demo.test' }),
      },
    });

    expect(screen.getByLabelText('Contraseña inicial')).toHaveAccessibleDescription(/8 caracteres.*72 bytes/);
    await user.type(screen.getByLabelText('Nombre completo'), '  Ana López ');
    await user.type(screen.getByLabelText('Correo electrónico'), '  Ana.Lopez@Demo.TEST ');
    await user.selectOptions(screen.getByLabelText('Rol'), 'Operador');
    await user.type(screen.getByLabelText('Contraseña inicial'), 'segura-123');
    await user.click(screen.getByRole('button', { name: 'Dar de alta' }));

    expect(await screen.findByText('Has dado de alta a Ana López.')).toBeInTheDocument();
    expect(bodyOf(fetchMock, CREATE)).toEqual({
      email: 'ana.lopez@demo.test',
      full_name: 'Ana López',
      role: 'operator',
      password: 'segura-123',
    });
    expect(cellsOf('Ana López')[1]).toHaveTextContent('Operador');
    expect(screen.getByLabelText('Nombre completo')).toHaveValue('');
    expect(screen.getByLabelText('Contraseña inicial')).toHaveValue('');
    expect(screen.getByLabelText('Rol')).toHaveValue('viewer');
  });

  it('checks the fields before sending', async () => {
    const { user, fetchMock } = await openTeam('admin');

    await user.click(screen.getByRole('button', { name: 'Dar de alta' }));
    expect(screen.getByLabelText('Nombre completo')).toHaveAccessibleDescription('Indica el nombre completo.');
    expect(screen.getByLabelText('Correo electrónico')).toHaveAccessibleDescription(
      'Indica el correo electrónico.',
    );
    expect(screen.getByLabelText('Nombre completo')).toHaveFocus();

    await user.type(screen.getByLabelText('Nombre completo'), 'Ana López');
    await user.type(screen.getByLabelText('Correo electrónico'), 'ana@demo.test');
    await user.type(screen.getByLabelText('Contraseña inicial'), 'corta');
    await user.click(screen.getByRole('button', { name: 'Dar de alta' }));
    expect(screen.getByLabelText('Contraseña inicial')).toHaveAccessibleDescription(
      /La contraseña debe tener al menos 8 caracteres\./,
    );

    // 37 characters, but 74 UTF-8 bytes.
    await user.clear(screen.getByLabelText('Contraseña inicial'));
    await user.type(screen.getByLabelText('Contraseña inicial'), 'ñ'.repeat(37));
    await user.click(screen.getByRole('button', { name: 'Dar de alta' }));
    expect(screen.getByLabelText('Contraseña inicial')).toHaveAccessibleDescription(/no puede superar los 72 bytes/);
    expect(screen.getByLabelText('Contraseña inicial')).toHaveFocus();
    expect(routesCalled(fetchMock)).not.toContain(CREATE);
  });

  it('explains a duplicate email (409) and invalid data (422) in Spanish', async () => {
    const replies: FakeReply[] = [
      { status: 409, body: { detail: 'email already registered' } },
      { status: 422, body: { detail: [{ msg: 'Value error, invalid email address' }] } },
    ];
    const { user } = await openTeam('admin', { [CREATE]: () => replies.shift()! });

    await user.type(screen.getByLabelText('Nombre completo'), 'Óscar Bis');
    await user.type(screen.getByLabelText('Correo electrónico'), 'operator@demo.test');
    await user.type(screen.getByLabelText('Contraseña inicial'), 'segura-123');
    await user.click(screen.getByRole('button', { name: 'Dar de alta' }));

    const email = screen.getByLabelText('Correo electrónico');
    await waitFor(() =>
      expect(email).toHaveAccessibleDescription('Ya hay una persona con este correo electrónico en tu empresa.'),
    );
    expect(email).toHaveFocus();

    await user.clear(email);
    await user.type(email, 'otro@demo.test');
    await user.click(screen.getByRole('button', { name: 'Dar de alta' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Los datos no son válidos. Revisa los campos e inténtalo de nuevo.');
    expect(document.body).not.toHaveTextContent(/already registered|Value error/);
  });

  it('does not offer the owner role to an admin', async () => {
    const { user } = await openTeam('admin');

    expect(optionsOf(screen.getByLabelText('Rol'))).toEqual(['Lector', 'Operador', 'Administrador']);
    await user.click(screen.getByRole('button', { name: 'Cambiar rol «Óscar Operario»' }));
    expect(optionsOf(screen.getByLabelText('Nuevo rol'))).toEqual(['Lector', 'Operador', 'Administrador']);
  });

  it('offers every role to an owner', async () => {
    await openTeam('owner');

    expect(optionsOf(screen.getByLabelText('Rol'))).toEqual(['Lector', 'Operador', 'Administrador', 'Propietario']);
  });
});

describe('TeamPage: changing a member', () => {
  it('changes a role after confirming, and Cancelar sends nothing', async () => {
    const { user, fetchMock } = await openTeam('admin', {
      'PUT /api/v1/users/3/role': {
        status: 200,
        body: member({ id: 3, full_name: 'Óscar Operario', role: 'admin', email: 'operator@demo.test' }),
      },
    });
    const open = within(rowOf('Óscar Operario')).getByRole('button', { name: 'Cambiar rol «Óscar Operario»' });

    await user.click(open);
    const panel = screen.getByRole('region', { name: 'Cambiar el rol de Óscar Operario' });
    expect(within(panel).getByRole('heading', { name: 'Cambiar el rol de Óscar Operario' })).toHaveFocus();
    expect(screen.getByLabelText('Nuevo rol')).toHaveValue('operator');
    await user.click(within(panel).getByRole('button', { name: 'Cancelar' }));
    expect(screen.queryByRole('region', { name: /Cambiar el rol/ })).not.toBeInTheDocument();
    expect(open).toHaveFocus();

    await user.click(open);
    await user.selectOptions(screen.getByLabelText('Nuevo rol'), 'Administrador');
    await user.click(
      within(screen.getByRole('region', { name: 'Cambiar el rol de Óscar Operario' })).getByRole('button', {
        name: 'Cambiar rol',
      }),
    );

    expect(await screen.findByText('Has cambiado el rol de Óscar Operario a Administrador.')).toBeInTheDocument();
    expect(cellsOf('Óscar Operario')[1]).toHaveTextContent(/^Administrador$/);
    expect(bodyOf(fetchMock, 'PUT /api/v1/users/3/role')).toEqual({ role: 'admin' });
    expect(routesCalled(fetchMock)).toEqual([ME, LIST, 'PUT /api/v1/users/3/role']);
    expect(within(rowOf('Óscar Operario')).getByRole('button', { name: 'Cambiar rol «Óscar Operario»' })).toHaveFocus();
  });

  it('deactivates a member only after confirming', async () => {
    const { user, fetchMock } = await openTeam('admin', {
      'POST /api/v1/users/3/deactivate': {
        status: 200,
        body: member({ id: 3, full_name: 'Óscar Operario', role: 'operator', is_active: false }),
      },
    });

    await user.click(within(rowOf('Óscar Operario')).getByRole('button', { name: 'Desactivar «Óscar Operario»' }));
    const panel = screen.getByRole('region', { name: 'Desactivar a Óscar Operario' });
    expect(panel).toHaveTextContent('No podrá iniciar sesión hasta que reactives su cuenta.');
    expect(routesCalled(fetchMock)).toEqual([ME, LIST]);

    await user.click(within(panel).getByRole('button', { name: 'Desactivar' }));

    expect(await screen.findByText('Has desactivado la cuenta de Óscar Operario.')).toBeInTheDocument();
    expect(cellsOf('Óscar Operario')[2]).toHaveTextContent(/Inactivo$/);
    expect(within(rowOf('Óscar Operario')).getByRole('button', { name: 'Reactivar «Óscar Operario»' })).toHaveFocus();
    expect(routesCalled(fetchMock)).toEqual([ME, LIST, 'POST /api/v1/users/3/deactivate']);
  });

  it('reactivates a member', async () => {
    const { user } = await openTeam('admin', {
      'POST /api/v1/users/4/reactivate': {
        status: 200,
        body: member({ id: 4, full_name: 'Vera Consulta', role: 'viewer', is_active: true }),
      },
    });

    await user.click(within(rowOf('Vera Consulta')).getByRole('button', { name: 'Reactivar «Vera Consulta»' }));

    expect(await screen.findByText('Has reactivado la cuenta de Vera Consulta.')).toBeInTheDocument();
    expect(cellsOf('Vera Consulta')[2]).toHaveTextContent(/Activo$/);
    expect(within(rowOf('Vera Consulta')).getByRole('button', { name: 'Desactivar «Vera Consulta»' })).toBeInTheDocument();
  });

  it.each<[string, FakeReply, string]>([
    [
      'the last active owner (409)',
      { status: 409, body: { detail: 'tenant must keep at least one active owner' } },
      'La empresa debe tener al menos un propietario activo.',
    ],
    [
      'your own account (409)',
      { status: 409, body: { detail: 'cannot deactivate yourself' } },
      'No puedes desactivar tu propia cuenta.',
    ],
    [
      'an owner-only change (403)',
      { status: 403, body: { detail: 'forbidden' } },
      'No tienes permiso para hacer este cambio. Solo un propietario puede gestionar a otros propietarios.',
    ],
    [
      'a member who no longer exists (404)',
      { status: 404, body: { detail: 'user not found' } },
      'Esa persona ya no está en tu empresa.',
    ],
  ])('explains a refusal for %s in Spanish', async (_case, reply, message) => {
    const { user } = await openTeam('owner', { 'POST /api/v1/users/2/deactivate': reply });

    await user.click(within(rowOf('Paula Fundadora')).getByRole('button', { name: 'Desactivar «Paula Fundadora»' }));
    await user.click(
      within(screen.getByRole('region', { name: 'Desactivar a Paula Fundadora' })).getByRole('button', {
        name: 'Desactivar',
      }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(message);
    expect(alert).not.toHaveTextContent(String((reply.body as { detail: string }).detail));
    expect(cellsOf('Paula Fundadora')[2]).toHaveTextContent(/Activo$/);
  });

  it('maps a refused role change of your own account (409)', async () => {
    const { user } = await openTeam('owner', {
      'PUT /api/v1/users/3/role': { status: 409, body: { detail: 'cannot change your own role' } },
    });

    await user.click(within(rowOf('Óscar Operario')).getByRole('button', { name: 'Cambiar rol «Óscar Operario»' }));
    await user.selectOptions(screen.getByLabelText('Nuevo rol'), 'Lector');
    await user.click(
      within(screen.getByRole('region', { name: 'Cambiar el rol de Óscar Operario' })).getByRole('button', {
        name: 'Cambiar rol',
      }),
    );

    expect(await screen.findByRole('alert')).toHaveTextContent('No puedes cambiar tu propio rol.');
  });

  it('has no axe violations with a confirmation open and form errors shown', { timeout: AXE_TEST_TIMEOUT_MS }, async () => {
    const { user } = await openTeam('admin');

    await user.click(screen.getByRole('button', { name: 'Dar de alta' }));
    await user.click(within(rowOf('Óscar Operario')).getByRole('button', { name: 'Desactivar «Óscar Operario»' }));
    await expectNoAxeViolations();
  });
});
