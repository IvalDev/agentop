import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import {
  fetchCurrentUser,
  requestLogout,
  requestRefreshToken,
  requestToken,
  type Credentials,
  type CurrentUser,
} from '../api/auth';
import { ApiError, createApiClient, type ApiClient } from '../api/client';
import { memoryTokenStorage, type TokenStorage } from './tokenStorage';

/** Why the user is on the login page, shown there as a status message. */
export type SessionNotice = 'expired' | 'loggedOut';

type SessionState =
  /** Checking the refresh cookie; no screen commits to a session yet. */
  | { status: 'restoring'; user: null; notice: null }
  /** The check could not complete (network or server failure): kept for a retry. */
  | { status: 'unavailable'; user: null; notice: null }
  | { status: 'anonymous'; user: null; notice: SessionNotice | null }
  | { status: 'authenticated'; user: CurrentUser; notice: null };

export type SessionValue = SessionState & {
  /** API client bound to this session: use it for every authenticated call. */
  api: ApiClient;
  /** Throws `ApiError` (401 for bad credentials, 0 when the server is unreachable). */
  login(credentials: Credentials): Promise<void>;
  logout(): void;
  /** Check the refresh cookie again after an `unavailable` restore. */
  retryRestore(): void;
};

const SessionContext = createContext<SessionValue | null>(null);

const RESTORING: SessionState = { status: 'restoring', user: null, notice: null };

/** No answer, a timeout, rate limiting, or a server error: worth retrying. */
function isTransient(error: unknown): boolean {
  if (!(error instanceof ApiError)) {
    return false;
  }
  return error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500;
}

/** The access token in memory, mirrored in `storage` (a seam for tests). */
function createTokenHolder(storage: TokenStorage) {
  let token: string | null = null;
  return {
    get: () => token,
    set(next: string) {
      token = next;
      storage.write(next);
    },
    clear() {
      token = null;
      storage.clear();
    },
  };
}

/** Latest session refresher, reachable from the API client without re-creating it. */
function createRefreshHolder() {
  let refresh: (() => Promise<string | null>) | null = null;
  return {
    set(next: () => Promise<string | null>) {
      refresh = next;
    },
    call: () => refresh?.() ?? Promise.resolve(null),
  };
}

export interface SessionProviderProps {
  children: ReactNode;
  storage?: TokenStorage;
}

/**
 * Holds the access token in memory and the current user from
 * `GET /api/v1/auth/me`. On mount it restores the session silently: the
 * httpOnly refresh cookie identifies this browser to `POST /api/v1/auth/refresh`,
 * so a reload (or the desktop widget restarting) never asks for the password
 * while the cookie lasts. The client refreshes once mid-session on a 401.
 */
export function SessionProvider({ children, storage = memoryTokenStorage }: SessionProviderProps) {
  const [tokens] = useState(() => createTokenHolder(storage));
  const [state, setState] = useState<SessionState>(RESTORING);
  const [restoreAttempt, setRestoreAttempt] = useState(0);
  const [refresh] = useState(createRefreshHolder);

  const end = useCallback(
    (notice: SessionNotice | null) => {
      tokens.clear();
      setState({ status: 'anonymous', user: null, notice });
    },
    [tokens],
  );

  const api = useMemo(
    () =>
      createApiClient({
        getToken: tokens.get,
        onUnauthorized: () => end('expired'),
        refresh: refresh.call,
      }),
    [tokens, refresh, end],
  );

  const refreshSession = useCallback(async (): Promise<string> => {
    const token = await requestRefreshToken(api);
    tokens.set(token);
    return token;
  }, [api, tokens]);

  // The client's 401 handler must call the current callback, which needs `api`.
  useEffect(() => {
    refresh.set(refreshSession);
  }, [refresh, refreshSession]);

  useEffect(() => {
    let active = true;
    const restore = async () => {
      try {
        const token = await refreshSession();
        const user = await fetchCurrentUser(api, token);
        if (active) {
          setState({ status: 'authenticated', user, notice: null });
        }
      } catch (error) {
        if (!active) {
          return;
        }
        if (isTransient(error)) {
          setState({ status: 'unavailable', user: null, notice: null });
        } else {
          // No valid cookie (401) or no refresh endpoint: merely anonymous.
          end(null);
        }
      }
    };
    void restore();
    return () => {
      active = false;
    };
  }, [api, end, refreshSession, restoreAttempt]);

  const retryRestore = useCallback(() => {
    setState(RESTORING);
    setRestoreAttempt((attempt) => attempt + 1);
  }, []);

  const login = useCallback(
    async (credentials: Credentials) => {
      const token = await requestToken(api, credentials);
      const user = await fetchCurrentUser(api, token);
      tokens.set(token);
      setState({ status: 'authenticated', user, notice: null });
    },
    [api, tokens],
  );

  const logout = useCallback(() => {
    // Best effort: the server revokes this device's cookie, but a failure
    // never blocks the local logout.
    void requestLogout(api).catch(() => undefined);
    end('loggedOut');
  }, [api, end]);

  const value = useMemo<SessionValue>(
    () => ({ ...state, api, login, logout, retryRestore }),
    [state, api, login, logout, retryRestore],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error('useSession must be used inside <SessionProvider>');
  }
  return value;
}

/** The signed-in user; only valid below `RequireAuth`. */
export function useCurrentUser(): CurrentUser {
  const { user } = useSession();
  if (user === null) {
    throw new Error('useCurrentUser must be used below <RequireAuth>');
  }
  return user;
}
