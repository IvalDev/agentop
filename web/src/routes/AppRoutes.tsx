import { Navigate, Outlet, Route, Routes } from 'react-router';
import { ChatPage } from '../pages/ChatPage';
import { DashboardShell } from '../pages/DashboardShell';
import { KnowledgePage } from '../pages/KnowledgePage';
import { LoginPage } from '../pages/LoginPage';
import { NotFoundPage } from '../pages/NotFoundPage';
import { TeamPage } from '../pages/TeamPage';
import { TEAM_MANAGER_ROLES } from '../session/roles';
import { paths } from './paths';
import { RequireAuth } from './RequireAuth';
import { RequireRole } from './RequireRole';

export function AppRoutes() {
  return (
    <Routes>
      <Route path={paths.login} element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <DashboardShell />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to={paths.chat} replace />} />
        <Route path={paths.chat} element={<ChatPage />} />
        <Route path={paths.knowledge} element={<KnowledgePage />} />
        <Route
          path={paths.settings}
          element={
            <RequireRole allowed={TEAM_MANAGER_ROLES}>
              <Outlet />
            </RequireRole>
          }
        >
          <Route index element={<Navigate to={paths.team} replace />} />
          <Route path={paths.team} element={<TeamPage />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
