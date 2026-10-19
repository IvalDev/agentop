import { vi } from 'vitest';
import type { Role } from '../session/roles';

/** A canned answer: a status, an optional JSON body, and optional extra headers. */
export interface FakeReply {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
}

/**
 * Handlers keyed by `"<METHOD> <path>"`, e.g. `"GET /api/v1/auth/me"`. A handler may return a
 * promise, so a test can hold a request open and check what the page shows meanwhile.
 */
export type FakeRoutes = Record<string, FakeReply | ((init: RequestInit) => FakeReply | Promise<FakeReply>)>;

export function jsonResponse({ status, body, headers }: FakeReply): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
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
    return jsonResponse(typeof handler === 'function' ? await handler(init) : handler);
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

/** `"<METHOD> <path>"` of every recorded fetch call, in order. */
export function routesCalled(fetchMock: ReturnType<typeof installFakeApi>): string[] {
  return fetchMock.mock.calls.map(([input, init]) => `${(init?.method ?? 'GET').toUpperCase()} ${String(input)}`);
}

/**
 * Replies that wait until the test releases them, one at a time and in arrival order, to
 * check what the page shows while a request is in flight.
 */
export function createGate() {
  const waiting: Array<() => void> = [];
  return {
    hold(reply: () => FakeReply): Promise<FakeReply> {
      return new Promise((resolve) => waiting.push(() => resolve(reply())));
    },
    /** How many requests are waiting. */
    get pending(): number {
      return waiting.length;
    },
    /** Answer the oldest waiting request. */
    release(): void {
      const next = waiting.shift();
      if (next === undefined) {
        throw new Error('No request is waiting at the gate.');
      }
      next();
    },
  };
}

/** Header names and values of one recorded fetch call. */
export function headersOf(init: RequestInit | undefined): Headers {
  return new Headers(init?.headers);
}
