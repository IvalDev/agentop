/**
 * Where the access token survives a reload: `sessionStorage`, so it is scoped to
 * this tab and gone when the tab closes. Never `localStorage`. The API has no
 * refresh token yet; the follow-up is an httpOnly refresh cookie.
 */
export const TOKEN_STORAGE_KEY = 'admin-agent.access-token';

export interface TokenStorage {
  read(): string | null;
  write(token: string): void;
  clear(): void;
}

// Storage access can throw (privacy modes, blocked site data): degrade to memory only.
export const sessionTokenStorage: TokenStorage = {
  read() {
    try {
      return window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
    } catch {
      return null;
    }
  },
  write(token) {
    try {
      window.sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    } catch {
      // Memory only: a reload will ask for the login again.
    }
  },
  clear() {
    try {
      window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    } catch {
      // Nothing stored.
    }
  },
};
