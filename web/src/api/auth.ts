import { parseRole, type Role } from '../session/roles';
import type { ApiClient } from './client';

export interface Credentials {
  tenantSlug: string;
  email: string;
  password: string;
}

export interface CurrentUser {
  id: number;
  email: string;
  fullName: string;
  role: Role;
  tenantId: number;
}

interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

interface MeResponse {
  id: number;
  email: string;
  full_name: string;
  role: string;
  tenant_id: number;
}

/** The API compares emails exactly and stores them trimmed and lowercase. */
export function normalizeEmail(email: string): string {
  return email.trim().toLowerCase();
}

/** `POST /api/v1/auth/login`: returns the access token. Passwords are sent as typed. */
export async function requestToken(api: ApiClient, credentials: Credentials): Promise<string> {
  const body = await api.post<LoginResponse>(
    '/api/v1/auth/login',
    {
      email: normalizeEmail(credentials.email),
      password: credentials.password,
      tenant_slug: credentials.tenantSlug.trim(),
    },
    { auth: false },
  );
  return body.access_token;
}

/**
 * `POST /api/v1/auth/refresh`: renews the access token from the httpOnly
 * refresh cookie. Anonymous on purpose: no bearer token is sent, and a 401 is
 * just the "no session in this browser" answer.
 */
export async function requestRefreshToken(api: ApiClient): Promise<string> {
  const body = await api.post<LoginResponse>('/api/v1/auth/refresh', undefined, { auth: false });
  return body.access_token;
}

/** `POST /api/v1/auth/logout`: asks the server to revoke this device's session. */
export async function requestLogout(api: ApiClient): Promise<void> {
  await api.post<void>('/api/v1/auth/logout', undefined, { auth: false });
}

/** `GET /api/v1/auth/me` with an explicit token (a fresh login or a restored one). */
export async function fetchCurrentUser(api: ApiClient, token: string): Promise<CurrentUser> {
  const me = await api.get<MeResponse>('/api/v1/auth/me', { token });
  return {
    id: me.id,
    email: me.email,
    fullName: me.full_name,
    role: parseRole(me.role),
    tenantId: me.tenant_id,
  };
}
