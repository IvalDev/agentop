import { vi } from 'vitest';
import type { Role } from '../session/roles';

/** A canned answer: a status plus an optional JSON body. */
export interface FakeReply {
  status: number;
  body?: unknown;
}

/** Handlers keyed by `"<METHOD> <path>"`, e.g. `"GET /api/v1/auth/me"`. */
export type FakeRoutes = Record<string, FakeReply | ((init: RequestInit) => FakeReply)>;

export function jsonResponse({ status, body }: FakeReply): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Replace `fetch` with a router over canned replies; unknown routes answer 404.
 * Returns the mock so tests can inspect calls (`fetchMock.mock.calls`).
 */
export function installFakeApi(routes: FakeRoutes) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const method = (init.method ?? 'GET').toUpperCase();
    const handler = routes[`${method} ${String(input)}`];
    if (handler === undefined) {
      return jsonResponse({ status: 404, body: { detail: 'Not Found' } });
    }
    return jsonResponse(typeof handler === 'function' ? handler(init) : handler);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** `GET /api/v1/auth/me` body for a demo user of the given role. */
export function meBody(role: Role) {
  const names: Record<Role, string> = {
    owner: 'Olga Dueña',
    admin: 'Adrián Gestor',
    operator: 'Óscar Operario',
    viewer: 'Vera Consulta',
  };
  return { id: 1, email: `${role}@demo.test`, full_name: names[role], role, tenant_id: 1 };
}

/** Routes for a valid stored token belonging to a user of `role`. */
export function signedInRoutes(role: Role): FakeRoutes {
  return { 'GET /api/v1/auth/me': { status: 200, body: meBody(role) } };
}

/** Header names and values of one recorded fetch call. */
export function headersOf(init: RequestInit | undefined): Headers {
  return new Headers(init?.headers);
}
