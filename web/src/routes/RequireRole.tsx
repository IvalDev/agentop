import type { ReactNode } from 'react';
import { Navigate } from 'react-router';
import type { Role } from '../session/roles';
import { useCurrentUser } from '../session/SessionProvider';
import { paths } from './paths';

/**
 * Renders `children` only for the `allowed` roles; anyone else goes to the chat.
 * This only shapes the UI: the API enforces the same rule on every request.
 */
export function RequireRole({ allowed, children }: { allowed: readonly Role[]; children: ReactNode }) {
  const user = useCurrentUser();
  if (!allowed.includes(user.role)) {
    return <Navigate to={paths.chat} replace />;
  }
  return children;
}
