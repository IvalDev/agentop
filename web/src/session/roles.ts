export type Role = 'owner' | 'admin' | 'operator' | 'viewer';

const ROLES: readonly Role[] = ['owner', 'admin', 'operator', 'viewer'];

/** Roles that reach Settings (Team and Tools). The API enforces the same rule on every call. */
export const SETTINGS_ADMIN_ROLES: readonly Role[] = ['owner', 'admin'];

/** Roles that upload documents and run their ingest. The API enforces the same rule. */
export const DOCUMENT_EDITOR_ROLES: readonly Role[] = ['owner', 'admin', 'operator'];

/** Parse a role from the API; anything unknown gets the least privilege. */
export function parseRole(value: string): Role {
  return (ROLES as readonly string[]).includes(value) ? (value as Role) : 'viewer';
}

export function canManageSettings(role: Role): boolean {
  return SETTINGS_ADMIN_ROLES.includes(role);
}

export function canUploadDocuments(role: Role): boolean {
  return DOCUMENT_EDITOR_ROLES.includes(role);
}
