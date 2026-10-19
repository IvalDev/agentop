import { Navigate, Outlet, Route, Routes } from 'react-router';
import { ChatPage } from '../pages/ChatPage';
import { DashboardShell } from '../pages/DashboardShell';
import { KnowledgePage } from '../pages/KnowledgePage';
import { LoginPage } from '../pages/LoginPage';
import { MyToolsPage } from '../pages/MyToolsPage';
import { NotFoundPage } from '../pages/NotFoundPage';
import { OnboardingPage } from '../pages/OnboardingPage';
import { TeamPage } from '../pages/TeamPage';
import { ToolsPage } from '../pages/ToolsPage';
import { WidgetPage } from '../pages/WidgetPage';
import { SETTINGS_ADMIN_ROLES } from '../session/roles';
import { paths } from './paths';
import { RequireAuth } from './RequireAuth';
import { RequireRole } from './RequireRole';

export function AppRoutes() {
  return (
    <Routes>
      <Route path={paths.login} element={<LoginPage />} />
      {/* The desktop widget loads this route: chat only, no dashboard layout. */}
      <Route
        path={paths.widget}
        element={
          <RequireAuth>
            <WidgetPage />
          </RequireAuth>
        }
      />
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
        {/* Every user configures their own tools: these two are NOT admin-gated,
            or the OAuth callback would bounce a connected non-admin. */}
        <Route path={paths.myTools} element={<MyToolsPage />} />
        <Route path={paths.onboardingRoute} element={<OnboardingPage />} />
        <Route
          path={paths.settings}
          element={
            <RequireRole allowed={SETTINGS_ADMIN_ROLES}>
              <Outlet />
            </RequireRole>
          }
        >
          <Route index element={<Navigate to={paths.team} replace />} />
          <Route path={paths.team} element={<TeamPage />} />
          <Route path={paths.tools} element={<ToolsPage />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
