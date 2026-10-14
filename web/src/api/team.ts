import { parseRole, type Role } from '../session/roles';
import { normalizeEmail } from './auth';
import type { ApiClient } from './client';

/** A user of the caller's company (`TeamUserResponse`). */
export interface TeamMember {
  id: number;
  email: string;
  fullName: string;
  role: Role;
  isActive: boolean;
  /** ISO 8601 timestamp. */
  createdAt: string;
}

export interface NewMember {
  fullName: string;
  email: string;
  role: Role;
  password: string;
}

interface TeamUserBody {
  id: number;
  email: string;
  full_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

function toMember(body: TeamUserBody): TeamMember {
  return {
    id: body.id,
    email: body.email,
    fullName: body.full_name,
    role: parseRole(body.role),
    isActive: body.is_active,
    createdAt: body.created_at,
  };
}

/** `GET /api/v1/users` (owner and admin): every user of the company, oldest first. */
export async function listMembers(api: ApiClient): Promise<TeamMember[]> {
  const body = await api.get<{ users: TeamUserBody[] }>('/api/v1/users');
  return body.users.map(toMember);
}

/** `POST /api/v1/users`: the email is trimmed and lowercased, the name trimmed, the password sent as typed. */
export async function createMember(api: ApiClient, member: NewMember): Promise<TeamMember> {
  const body = await api.post<TeamUserBody>('/api/v1/users', {
    email: normalizeEmail(member.email),
    full_name: member.fullName.trim(),
    role: member.role,
    password: member.password,
  });
  return toMember(body);
}

/** `PUT /api/v1/users/{id}/role`. */
export async function changeMemberRole(api: ApiClient, id: number, role: Role): Promise<TeamMember> {
  return toMember(await api.put<TeamUserBody>(`/api/v1/users/${id}/role`, { role }));
}

/** `POST /api/v1/users/{id}/deactivate` or `/reactivate`. */
export async function setMemberActive(api: ApiClient, id: number, active: boolean): Promise<TeamMember> {
  const action = active ? 'reactivate' : 'deactivate';
  return toMember(await api.post<TeamUserBody>(`/api/v1/users/${id}/${action}`));
}
