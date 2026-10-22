import { describe, expect, it } from 'vitest';
import { installFakeApi } from '../test/fakeApi';
import { createApiClient } from './client';
import { createPairingCode, listConnectors, revokeConnector } from './connectors';

function client() {
  return createApiClient({ getToken: () => 'session-token', onUnauthorized: () => {} });
}

describe('connectors API', () => {
  it('creates a pairing code and parses the code and its expiry', async () => {
    const fetchMock = installFakeApi({
      'POST /api/v1/connectors/pairing-codes': {
        status: 201,
        body: { code: 'ABCD-EFGH', expires_at: '2026-10-20T12:10:00Z' },
      },
    });

    await expect(createPairingCode(client())).resolves.toEqual({
      code: 'ABCD-EFGH',
      expiresAt: '2026-10-20T12:10:00Z',
    });

    const [input, init] = fetchMock.mock.calls[0]!;
    expect(input).toBe('/api/v1/connectors/pairing-codes');
    expect(init?.method).toBe('POST');
  });

  it('lists the caller devices, mapping the snake_case fields', async () => {
    installFakeApi({
      'GET /api/v1/connectors': {
        status: 200,
        body: {
          connectors: [
            {
              id: 'conector-1',
              name: 'MacBook de Vera',
              connected: true,
              last_seen_at: '2026-10-20T12:00:00Z',
              created_at: '2026-10-19T09:00:00Z',
            },
            {
              id: 'conector-2',
              name: 'PC de la oficina',
              connected: false,
              last_seen_at: null,
              created_at: '2026-10-18T09:00:00Z',
            },
          ],
        },
      },
    });

    await expect(listConnectors(client())).resolves.toEqual([
      {
        id: 'conector-1',
        name: 'MacBook de Vera',
        connected: true,
        lastSeenAt: '2026-10-20T12:00:00Z',
        createdAt: '2026-10-19T09:00:00Z',
      },
      {
        id: 'conector-2',
        name: 'PC de la oficina',
        connected: false,
        lastSeenAt: null,
        createdAt: '2026-10-18T09:00:00Z',
      },
    ]);
  });

  it('revokes a device with DELETE and accepts the empty 204 reply', async () => {
    const fetchMock = installFakeApi({
      'DELETE /api/v1/connectors/conector-1': { status: 204 },
    });

    await expect(revokeConnector(client(), 'conector-1')).resolves.toBeUndefined();

    const [input, init] = fetchMock.mock.calls[0]!;
    expect(input).toBe('/api/v1/connectors/conector-1');
    expect(init?.method).toBe('DELETE');
  });

  it('keeps the API error (status and detail) for the page to map', async () => {
    installFakeApi({
      'GET /api/v1/connectors': { status: 500, body: { detail: 'internal error' } },
    });

    await expect(listConnectors(client())).rejects.toMatchObject({ status: 500, detail: 'internal error' });
  });

  it('rejects a malformed pairing code instead of showing undefined', async () => {
    installFakeApi({
      'POST /api/v1/connectors/pairing-codes': { status: 201, body: { code: 'ABCD-EFGH' } },
    });

    await expect(createPairingCode(client())).rejects.toThrow(TypeError);
  });
});
