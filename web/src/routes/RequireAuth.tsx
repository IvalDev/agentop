import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router';
import { LoadingScreen } from '../components/templates/LoadingScreen';
import { SessionUnavailableScreen } from '../components/templates/SessionUnavailableScreen';
import { useSession } from '../session/SessionProvider';
import { paths } from './paths';

/** Renders `children` only with a session; otherwise sends the visitor to the login page. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, retryRestore, logout } = useSession();
  const { pathname } = useLocation();
  if (status === 'restoring') {
    return <LoadingScreen />;
  }
  if (status === 'unavailable') {
    return <SessionUnavailableScreen onRetry={retryRestore} onLogout={logout} />;
  }
  if (status === 'anonymous') {
    return <Navigate to={paths.login} replace state={{ from: pathname }} />;
  }
  return children;
}
