import { describe, expect, it, vi } from 'vitest';
import { headersOf, installFakeApi } from '../test/fakeApi';
import { ApiError, createApiClient } from './client';

function clientWith(token: string | null, refresh?: () => Promise<string | null>) {
  const onUnauthorized = vi.fn();
  const api = createApiClient({ getToken: () => token, onUnauthorized, refresh });
  return { api, onUnauthorized };
}

describe('createApiClient', () => {
  it('sends the session token as a bearer header and parses JSON', async () => {
    const fetchMock = installFakeApi({ 'GET /api/v1/things': { status: 200, body: { ok: true } } });
    const { api } = clientWith('session-token');

    await expect(api.get('/api/v1/things')).resolves.toEqual({ ok: true });

    const [, init] = fetchMock.mock.calls[0]!;
    expect(headersOf(init).get('Authorization')).toBe('Bearer session-token');
  });

  it('serialises a body as JSON', async () => {
    const fetchMock = installFakeApi({ 'POST /api/v1/things': { status: 201, body: { id: 7 } } });
    const { api } = clientWith('session-token');

    await api.post('/api/v1/things', { name: 'x' });

    const [, init] = fetchMock.mock.calls[0]!;
    expect(headersOf(init).get('Content-Type')).toBe('application/json');
    expect(init?.body).toBe(JSON.stringify({ name: 'x' }));
  });

  it('turns an error reply into an ApiError carrying the `detail`', async () => {
    installFakeApi({ 'POST /api/v1/users': { status: 409, body: { detail: 'email already registered' } } });
    const { api, onUnauthorized } = clientWith('session-token');

    const error = await api.post('/api/v1/users', {}).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, detail: 'email already registered' });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('keeps a non-string `detail` (validation errors) as received', async () => {
    const detail = [{ loc: ['body', 'role'], msg: 'invalid', type: 'enum' }];
    installFakeApi({ 'PUT /api/v1/users/3/role': { status: 422, body: { detail } } });
    const { api } = clientWith('session-token');

    await expect(api.put('/api/v1/users/3/role', { role: 'boss' })).rejects.toMatchObject({
      status: 422,
      detail,
    });
  });

  it('reports a 401 on the session token to the session, then rejects', async () => {
    installFakeApi({ 'GET /api/v1/users': { status: 401, body: { detail: 'invalid token' } } });
    const { api, onUnauthorized } = clientWith('expired-token');

    await expect(api.get('/api/v1/users')).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('refreshes once on a session 401 and retries with the new token', async () => {
    let thingsCalls = 0;
    const fetchMock = installFakeApi({
      'GET /api/v1/things': () =>
        (thingsCalls += 1) === 1
          ? { status: 401, body: { detail: 'invalid token' } }
          : { status: 200, body: { ok: true } },
    });
    const refresh = vi.fn(async () => 'fresh-token');
    const { api, onUnauthorized } = clientWith('stale-token', refresh);

    await expect(api.get('/api/v1/things')).resolves.toEqual({ ok: true });

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(headersOf(fetchMock.mock.calls[0]?.[1]).get('Authorization')).toBe('Bearer stale-token');
    expect(headersOf(fetchMock.mock.calls[1]?.[1]).get('Authorization')).toBe('Bearer fresh-token');
  });

  it('does not loop: a retried 401 ends the session after the one refresh', async () => {
    const fetchMock = installFakeApi({
      'GET /api/v1/things': { status: 401, body: { detail: 'invalid token' } },
    });
    const refresh = vi.fn(async () => 'fresh-token');
    const { api, onUnauthorized } = clientWith('stale-token', refresh);

    await expect(api.get('/api/v1/things')).rejects.toMatchObject({ status: 401 });

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(headersOf(fetchMock.mock.calls[1]?.[1]).get('Authorization')).toBe('Bearer fresh-token');
  });

  it('falls through to onUnauthorized when the refresh fails', async () => {
    installFakeApi({ 'GET /api/v1/things': { status: 401, body: { detail: 'invalid token' } } });
    const refresh = vi.fn(async () => null);
    const { api, onUnauthorized } = clientWith('stale-token', refresh);

    await expect(api.get('/api/v1/things')).rejects.toMatchObject({ status: 401 });

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('treats a rejected refresh as an expired session, not a network error', async () => {
    installFakeApi({ 'GET /api/v1/things': { status: 401, body: { detail: 'invalid token' } } });
    const refresh = vi.fn(async () => {
      throw new Error('refresh exploded');
    });
    const { api, onUnauthorized } = clientWith('stale-token', refresh);

    await expect(api.get('/api/v1/things')).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('sends no token and ignores 401 for anonymous calls such as login', async () => {
    const fetchMock = installFakeApi({
      'POST /api/v1/auth/login': { status: 401, body: { detail: 'invalid credentials' } },
    });
    const { api, onUnauthorized } = clientWith('session-token');

    await expect(api.post('/api/v1/auth/login', {}, { auth: false })).rejects.toMatchObject({
      status: 401,
      detail: 'invalid credentials',
    });

    const [, init] = fetchMock.mock.calls[0]!;
    expect(headersOf(init).has('Authorization')).toBe(false);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('uses an explicit token without treating its 401 as the session expiring', async () => {
    const fetchMock = installFakeApi({ 'GET /api/v1/auth/me': { status: 401, body: { detail: 'invalid token' } } });
    const refresh = vi.fn(async () => 'fresh-token');
    const { api, onUnauthorized } = clientWith(null, refresh);

    await expect(api.get('/api/v1/auth/me', { token: 'candidate' })).rejects.toMatchObject({ status: 401 });

    const [, init] = fetchMock.mock.calls[0]!;
    expect(headersOf(init).get('Authorization')).toBe('Bearer candidate');
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('reports a network failure as an ApiError with status 0', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const { api } = clientWith('session-token');

    await expect(api.get('/api/v1/things')).rejects.toMatchObject({ status: 0, detail: null });
  });

  it.each([
    'https://evil.example/api/v1/things',
    '//evil.example/api/v1/things',
    '/\\evil.example/api/v1/things',
    '/healthz',
    'api/v1/things',
  ])('refuses %s: the token only ever goes to same-origin /api/ paths', async (path) => {
    const fetchMock = installFakeApi({});
    const { api } = clientWith('session-token');

    await expect(api.get(path)).rejects.toThrow(TypeError);
    await expect(api.post(path, {}, { auth: false })).rejects.toThrow(TypeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('returns the raw response from `send` for streaming bodies', async () => {
    installFakeApi({ 'POST /api/v1/chat/stream': { status: 200, body: { streamed: true } } });
    const { api } = clientWith('session-token');

    const response = await api.send('/api/v1/chat/stream', { method: 'POST', body: { message: 'hola' } });

    expect(response).toBeInstanceOf(Response);
    await expect(response.json()).resolves.toEqual({ streamed: true });
  });
});
