import { ApiError } from '../api/client';
import type { TeamMember } from '../api/team';
import type { Role } from '../session/roles';

/** Roles in the order the forms list them, least privilege first. */
const ROLES_ASCENDING: readonly Role[] = ['viewer', 'operator', 'admin', 'owner'];

/** Only an owner creates owners or makes someone an owner (`users_service.py`). */
export function assignableRoles(callerRole: Role): Role[] {
  return ROLES_ASCENDING.filter((role) => role !== 'owner' || callerRole === 'owner');
}

/** Why a row offers no actions: your own account, or an owner when you are not one. */
export type MemberRestriction = 'self' | 'owner' | null;

export function memberRestriction(caller: { id: number; role: Role }, member: TeamMember): MemberRestriction {
  if (member.id === caller.id) {
    return 'self';
  }
  return member.role === 'owner' && caller.role !== 'owner' ? 'owner' : null;
}

/** DOM id of a row's action button, so focus can return to it after a confirmation step. */
export function memberActionId(memberId: number, action: 'role' | 'state'): string {
  return `member-${memberId}-${action}`;
}

/** Why a team call failed; each maps to a catalog message, never to the server's text. */
export type TeamErrorKind =
  | 'forbidden'
  | 'notFound'
  | 'emailTaken'
  | 'selfRole'
  | 'selfDeactivate'
  | 'selfAccount'
  | 'lastOwner'
  | 'conflict'
  | 'invalid'
  | 'network'
  | 'unexpected';

/** The 409 rules, by the exact `detail` the API sends (`users_service.py`). */
const CONFLICTS = new Map<unknown, TeamErrorKind>([
  ['email already registered', 'emailTaken'],
  ['cannot change your own role', 'selfRole'],
  ['cannot deactivate yourself', 'selfDeactivate'],
  ['cannot reactivate yourself', 'selfAccount'],
  ['tenant must keep at least one active owner', 'lastOwner'],
]);

export function teamErrorKind(error: unknown): TeamErrorKind {
  if (!(error instanceof ApiError)) {
    return 'unexpected';
  }
  switch (error.status) {
    case 0:
    case 502:
    case 503:
    case 504:
      return 'network';
    case 403:
      return 'forbidden';
    case 404:
      return 'notFound';
    case 409:
      return CONFLICTS.get(error.detail) ?? 'conflict';
    case 422:
      return 'invalid';
    default:
      return 'unexpected';
  }
}

export const PASSWORD_MIN_CHARS = 8;
export const PASSWORD_MAX_BYTES = 72;

export type PasswordProblem = 'passwordTooShort' | 'passwordTooLong' | null;

/** The API counts characters (code points) for the minimum and UTF-8 bytes for the maximum. */
export function passwordProblem(password: string): PasswordProblem {
  if (Array.from(password).length < PASSWORD_MIN_CHARS) {
    return 'passwordTooShort';
  }
  return new TextEncoder().encode(password).length > PASSWORD_MAX_BYTES ? 'passwordTooLong' : null;
}
