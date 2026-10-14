import { AppRoutes } from './routes/AppRoutes';
import { SessionProvider } from './session/SessionProvider';

/** The whole app below a router (BrowserRouter in `main.tsx`, MemoryRouter in tests). */
export function App() {
  return (
    <SessionProvider>
      <AppRoutes />
    </SessionProvider>
  );
}
