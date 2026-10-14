import { useState } from 'react';
import { Navigate, useLocation } from 'react-router';
import type { Credentials } from '../api/auth';
import { ApiError } from '../api/client';
import { LoginForm } from '../components/organisms/LoginForm';
import { AuthLayout } from '../components/templates/AuthLayout';
import { LoadingScreen } from '../components/templates/LoadingScreen';
import { SessionUnavailableScreen } from '../components/templates/SessionUnavailableScreen';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';
import { paths } from '../routes/paths';
import { useSession } from '../session/SessionProvider';

function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return t('login.errors.invalidCredentials');
    }
    // 0: no answer at all; 502–504: the dev proxy or a gateway could not reach the API.
    if (error.status === 0 || (error.status >= 502 && error.status <= 504)) {
      return t('login.errors.network');
    }
  }
  return t('login.errors.unexpected');
}

/**
 * Where to go after login: the protected page that sent us here, else the chat.
 * Browsers read `\` as `/`, so `/\host` is as foreign as `//host`.
 */
function returnPath(state: unknown): string {
  const from = (state as { from?: unknown } | null)?.from;
  const isLocalPath = typeof from === 'string' && /^\/(?![/\\])/.test(from);
  return isLocalPath && from !== paths.login ? from : paths.chat;
}

export function LoginPage() {
  const session = useSession();
  const location = useLocation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  usePageTitle(t('login.title'));

  if (session.status === 'restoring') {
    return <LoadingScreen />;
  }
  if (session.status === 'unavailable') {
    return <SessionUnavailableScreen onRetry={session.retryRestore} onLogout={session.logout} />;
  }
  if (session.status === 'authenticated') {
    return <Navigate to={returnPath(location.state)} replace />;
  }

  async function handleSubmit(credentials: Credentials) {
    setBusy(true);
    setError(null);
    try {
      await session.login(credentials);
    } catch (caught) {
      setError(loginErrorMessage(caught));
      setBusy(false);
    }
  }

  const notice = session.notice ? t(`login.notices.${session.notice}`) : null;
  return (
    <AuthLayout title={t('login.title')} intro={t('login.intro')}>
      <LoginForm onSubmit={handleSubmit} busy={busy} error={error} notice={notice} />
    </AuthLayout>
  );
}
