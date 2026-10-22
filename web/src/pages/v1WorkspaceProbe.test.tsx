/**
 * Independent V1 probes for the local-workspace card (S2 of the L1 contract):
 * the web renders device name + connection state and the pairing code, revokes
 * with confirmation, and never renders a folder path — even when a hypothetical
 * leaky server puts one in the reply.
 */
import { screen, waitFor } from '@testing-library/react';
import { expect, it } from 'vitest';
import {
  installFakeApi,
  routesCalled,
  signedInRoutes,
  type FakeReply,
  type FakeRoutes,
} from '../test/fakeApi';
import { formatDate } from '../i18n';
import { renderApp } from '../test/renderApp';

const LIST = 'GET /api/v1/modules';
const CONNECTORS = 'GET /api/v1/connectors';
const PAIRING = 'POST /api/v1/connectors/pairing-codes';
const DELETE = 'DELETE /api/v1/connectors/conector-1';
const ROUTE = '/configuracion/mis-herramientas';
const SECRET_PATH = '/home/usuario/documentos-secretos';

function connectorsReply(connectors: unknown[]): FakeReply {
  return { status: 200, body: { connectors } };
}

async function open(routes: FakeRoutes = {}) {
  const fetchMock = installFakeApi({
    ...signedInRoutes('viewer'),
    [LIST]: { status: 200, body: { modules: [] } },
    [CONNECTORS]: connectorsReply([]),
    ...routes,
  });
  const view = renderApp({ route: ROUTE, storedToken: 'viewer-token' });
  await screen.findByRole('heading', { name: 'Espacio de trabajo local' });
  return { ...view, fetchMock };
}

const LAPTOP = {
  id: 'conector-1',
  name: 'MacBook de Vera',
  connected: true,
  last_seen_at: '2026-10-20T12:00:00Z',
  created_at: '2026-10-19T09:00:00Z',
};

it('never renders a folder path, even if the server leaks one in extra fields', async () => {
  await open({
    [CONNECTORS]: connectorsReply([
      { ...LAPTOP, folder_path: SECRET_PATH, path: SECRET_PATH, files: ['notas.md'] },
    ]),
  });

  expect(await screen.findByText('MacBook de Vera')).toBeInTheDocument();
  expect(screen.getByText('Conectado')).toBeInTheDocument();
  expect(document.querySelector('time')).not.toBeNull();
  expect(document.body.textContent).not.toContain(SECRET_PATH);
  expect(document.body.textContent).not.toContain('documentos-secretos');
  expect(document.body.textContent).not.toContain('notas.md');
});

it('generates a XXXX-XXXX pairing code with its expiry, never a path', async () => {
  const { user, fetchMock } = await open({
    [PAIRING]: {
      status: 201,
      body: { code: 'ABCD-EFGH', expires_at: '2099-10-20T12:10:00Z' },
    },
  });

  await user.click(screen.getByRole('button', { name: 'Generar código' }));

  expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument();
  expect(
    screen.getByText(
      `El código caduca el ${formatDate('2099-10-20T12:10:00Z', { dateStyle: 'medium', timeStyle: 'short' })}.`,
    ),
  ).toBeInTheDocument();
  expect(document.body.textContent).not.toContain(SECRET_PATH);
  expect(routesCalled(fetchMock)).toContain(PAIRING);
});

it('revokes a device only after confirmation, via DELETE, with no path anywhere', async () => {
  const { user, fetchMock } = await open({
    [CONNECTORS]: connectorsReply([{ ...LAPTOP, connected: false, last_seen_at: null }]),
    [DELETE]: { status: 204 },
  });

  await user.click(await screen.findByRole('button', { name: 'Revocar «MacBook de Vera»' }));
  // Before confirming nothing is sent.
  expect(routesCalled(fetchMock)).not.toContain(DELETE);

  const panel = screen.getByRole('region', { name: 'Revocar «MacBook de Vera»' });
  const { within } = await import('@testing-library/react');
  await user.click(within(panel).getByRole('button', { name: 'Revocar' }));

  await waitFor(() => expect(routesCalled(fetchMock)).toContain(DELETE));
  expect(
    await screen.findByText('Has revocado el acceso de «MacBook de Vera».'),
  ).toBeInTheDocument();
  expect(document.body.textContent).not.toContain(SECRET_PATH);
});
