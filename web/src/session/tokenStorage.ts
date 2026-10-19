/**
 * Where the access token lives: in memory only, for the lifetime of the page.
 * Nothing token-shaped is written to `sessionStorage` or `localStorage`; a
 * reload restores the session with `POST /api/v1/auth/refresh`, carrying the
 * httpOnly refresh cookie that JavaScript cannot read. The `TokenStorage`
 * seam stays so tests (or a future client) can inject their own storage.
 */
export interface TokenStorage {
  read(): string | null;
  write(token: string): void;
  clear(): void;
}

let token: string | null = null;

/** Process-wide in-memory storage: gone on reload, unreachable from other origins. */
export const memoryTokenStorage: TokenStorage = {
  read: () => token,
  write(next) {
    token = next;
  },
  clear() {
    token = null;
  },
};
