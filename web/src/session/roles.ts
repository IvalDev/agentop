export type Role = 'owner' | 'admin' | 'operator' | 'viewer';

const ROLES: readonly Role[] = ['owner', 'admin', 'operator', 'viewer'];

/** Roles that see Settings → Team. The API enforces the same rule on every call. */
export const TEAM_MANAGER_ROLES: readonly Role[] = ['owner', 'admin'];

/** Roles that upload documents and run their ingest. The API enforces the same rule. */
export const DOCUMENT_EDITOR_ROLES: readonly Role[] = ['owner', 'admin', 'operator'];

/** Parse a role from the API; anything unknown gets the least privilege. */
export function parseRole(value: string): Role {
  return (ROLES as readonly string[]).includes(value) ? (value as Role) : 'viewer';
}

export function canManageTeam(role: Role): boolean {
  return TEAM_MANAGER_ROLES.includes(role);
}

export function canUploadDocuments(role: Role): boolean {
  return DOCUMENT_EDITOR_ROLES.includes(role);
}
