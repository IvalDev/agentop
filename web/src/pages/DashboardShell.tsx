import { useEffect, useRef } from 'react';
import { Outlet, useLocation } from 'react-router';
import { AppHeader } from '../components/organisms/AppHeader';
import { MainNav } from '../components/organisms/MainNav';
import { DashboardLayout } from '../components/templates/DashboardLayout';
import { canManageTeam } from '../session/roles';
import { useCurrentUser, useSession } from '../session/SessionProvider';

/** Route layout for every signed-in page: header, role-aware navigation, and the section. */
export function DashboardShell() {
  const user = useCurrentUser();
  const { logout } = useSession();
  const { pathname } = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const shownPath = useRef(pathname);

  // After client-side navigation, move focus to the new content (not on first load).
  useEffect(() => {
    if (shownPath.current !== pathname) {
      shownPath.current = pathname;
      mainRef.current?.focus();
    }
  }, [pathname]);

  return (
    <DashboardLayout
      mainRef={mainRef}
      header={
        <AppHeader
          userName={user.fullName}
          role={user.role}
          onLogout={logout}
          navigation={<MainNav showTeam={canManageTeam(user.role)} />}
        />
      }
    >
      <Outlet />
    </DashboardLayout>
  );
}
