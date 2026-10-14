import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { fetchCurrentUser, requestToken, type Credentials, type CurrentUser } from '../api/auth';
import { ApiError, createApiClient, type ApiClient } from '../api/client';
import { sessionTokenStorage, type TokenStorage } from './tokenStorage';

/** Why the user is on the login page, shown there as a status message. */
export type SessionNotice = 'expired' | 'loggedOut';

type SessionState =
  | { status: 'restoring'; user: null; notice: null }
  | { status: 'anonymous'; user: null; notice: SessionNotice | null }
  | { status: 'authenticated'; user: CurrentUser; notice: null };

export type SessionValue = SessionState & {
  /** API client bound to this session: use it for every authenticated call. */
  api: ApiClient;
  /** Throws `ApiError` (401 for bad credentials, 0 when the server is unreachable). */
  login(credentials: Credentials): Promise<void>;
  logout(): void;
};

const SessionContext = createContext<SessionValue | null>(null);

const ANONYMOUS: SessionState = { status: 'anonymous', user: null, notice: null };

/** The access token in memory, mirrored in `storage` so a reload can restore it. */
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

export interface SessionProviderProps {
  children: ReactNode;
  storage?: TokenStorage;
}

/**
 * Holds the access token in memory (mirrored in `sessionStorage` to survive a
 * reload) and the current user from `GET /api/v1/auth/me`.
 */
export function SessionProvider({ children, storage = sessionTokenStorage }: SessionProviderProps) {
  const [tokens] = useState(() => createTokenHolder(storage));
  const [state, setState] = useState<SessionState>(() =>
    storage.read() ? { status: 'restoring', user: null, notice: null } : ANONYMOUS,
  );

  const end = useCallback(
    (notice: SessionNotice | null) => {
      tokens.clear();
      setState({ status: 'anonymous', user: null, notice });
    },
    [tokens],
  );

  const api = useMemo(
    () => createApiClient({ getToken: tokens.get, onUnauthorized: () => end('expired') }),
    [tokens, end],
  );

  useEffect(() => {
    const stored = storage.read();
    if (!stored) {
      return;
    }
    let active = true;
    fetchCurrentUser(api, stored).then(
      (user) => {
        if (active) {
          tokens.set(stored);
          setState({ status: 'authenticated', user, notice: null });
        }
      },
      (error: unknown) => {
        if (active) {
          end(error instanceof ApiError && error.status === 401 ? 'expired' : null);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api, end, storage, tokens]);

  const login = useCallback(
    async (credentials: Credentials) => {
      const token = await requestToken(api, credentials);
      const user = await fetchCurrentUser(api, token);
      tokens.set(token);
      setState({ status: 'authenticated', user, notice: null });
    },
    [api, tokens],
  );

  const logout = useCallback(() => end('loggedOut'), [end]);

  const value = useMemo<SessionValue>(() => ({ ...state, api, login, logout }), [state, api, login, logout]);
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
