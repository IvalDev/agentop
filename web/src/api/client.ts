/**
 * Typed fetch wrapper for the same-origin API (`/api/...`).
 *
 * - Only calls same-origin paths starting with `/api/`: an absolute URL, a
 *   protocol-relative `//host`, or any other path is a programming error, so the
 *   token can never reach another origin or a non-API page.
 * - Adds `Authorization: Bearer <token>` with the session token (or an explicit one).
 * - Turns every non-2xx reply into an `ApiError` carrying FastAPI's `detail`.
 * - A 401 on the session token attempts ONE refresh (shared by concurrent 401s)
 *   and retries the original request with the new token. If the refresh fails or
 *   the retry is rejected again, `onUnauthorized` runs: the session provider
 *   clears it and the app returns to the login page.
 */

/** An HTTP error from the API, or a network failure (`status` 0). */
export class ApiError extends Error {
  readonly status: number;
  /** FastAPI's `detail`: a string for domain errors, a list for validation (422). */
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  /** JSON-serialised unless it is `FormData` (file uploads). */
  body?: unknown;
  /** `false` for anonymous calls such as login: no token, and a 401 is just an error. */
  auth?: boolean;
  /** Use this token instead of the session's; its 401 does not end the session. */
  token?: string;
}

export interface ApiClient {
  /** Low level: the raw `Response` after error handling, e.g. to read a stream. */
  send(path: string, options?: RequestOptions): Promise<Response>;
  /** Send and parse the JSON reply (`undefined` for 204). */
  request<T>(path: string, options?: RequestOptions): Promise<T>;
  get<T>(path: string, options?: RequestOptions): Promise<T>;
  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>;
  put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>;
}

export interface ApiClientConfig {
  getToken: () => string | null;
  onUnauthorized: () => void;
  /**
   * Renew the session after a 401 on the session token: resolves with a new
   * access token, or `null`/a rejection when the refresh itself failed.
   * Concurrent 401s share one in-flight call (single flight).
   */
  refresh?: () => Promise<string | null>;
}

async function readDetail(response: Response): Promise<unknown> {
  try {
    const body: unknown = await response.json();
    return typeof body === 'object' && body !== null && 'detail' in body ? body.detail : null;
  } catch {
    return null;
  }
}

/** `/api/` followed by a path: never `//host`, `/\host`, or a URL with a scheme. */
const API_PATH = /^\/api\/(?![/\\])/;

function assertApiPath(path: string): void {
  if (!API_PATH.test(path)) {
    throw new TypeError(`The API client only calls same-origin /api/ paths, not "${path}".`);
  }
}

export function createApiClient({ getToken, onUnauthorized, refresh }: ApiClientConfig): ApiClient {
  let refreshInFlight: Promise<string | null> | null = null;

  /** Single flight: every 401 while a refresh is running awaits the same one. */
  function refreshOnce(): Promise<string | null> {
    if (refresh === undefined) {
      return Promise.resolve(null);
    }
    refreshInFlight ??= refresh().finally(() => {
      refreshInFlight = null;
    });
    return refreshInFlight;
  }

  /** One fetch attempt. `overrideToken` carries the renewed token after a 401. */
  async function sendOnce(
    path: string,
    options: RequestOptions,
    overrideToken?: string,
  ): Promise<Response> {
    assertApiPath(path);
    const { body, auth = true, token, headers, ...init } = options;
    const finalHeaders = new Headers(headers);
    let payload: BodyInit | undefined;
    if (body instanceof FormData) {
      payload = body;
    } else if (body !== undefined) {
      payload = JSON.stringify(body);
      finalHeaders.set('Content-Type', 'application/json');
    }
    const bearer = auth ? (overrideToken ?? token ?? getToken()) : null;
    if (bearer) {
      finalHeaders.set('Authorization', `Bearer ${bearer}`);
    }

    try {
      return await fetch(path, { ...init, headers: finalHeaders, body: payload });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw error;
      }
      throw new ApiError(0, null);
    }
  }

  async function send(path: string, options: RequestOptions = {}): Promise<Response> {
    const { auth = true, token } = options;
    const usesSessionToken = auth && token === undefined;

    let response = await sendOnce(path, options);
    if (response.status === 401 && usesSessionToken) {
      let renewed: string | null = null;
      try {
        renewed = await refreshOnce();
      } catch {
        renewed = null;
      }
      if (renewed !== null) {
        response = await sendOnce(path, options, renewed);
      }
      if (response.status === 401) {
        onUnauthorized();
      }
    }
    if (response.ok) {
      return response;
    }
    const detail = await readDetail(response);
    throw new ApiError(response.status, detail);
  }

  async function request<T>(path: string, options?: RequestOptions): Promise<T> {
    const response = await send(path, options);
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  return {
    send,
    request,
    get: (path, options) => request(path, { ...options, method: 'GET' }),
    post: (path, body, options) => request(path, { ...options, method: 'POST', body }),
    put: (path, body, options) => request(path, { ...options, method: 'PUT', body }),
  };
}
