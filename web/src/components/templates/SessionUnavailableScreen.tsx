import { usePageTitle } from '../../hooks/usePageTitle';
import { t } from '../../i18n';
import { Alert } from '../atoms/Alert';
import { Button } from '../atoms/Button';

export interface SessionUnavailableScreenProps {
  onRetry: () => void;
  onLogout: () => void;
}

/** A stored session could not be checked (network or server failure): retry, or leave it. */
export function SessionUnavailableScreen({ onRetry, onLogout }: SessionUnavailableScreenProps) {
  const title = t('session.unavailable.title');
  usePageTitle(title);
  return (
    <main className="auth">
      <div className="auth__panel">
        <h1 className="auth__title">{title}</h1>
        <Alert tone="error">{t('session.unavailable.message')}</Alert>
        <div className="actions">
          <Button onClick={onRetry}>{t('session.unavailable.retry')}</Button>
          <Button variant="secondary" onClick={onLogout}>
            {t('header.logout')}
          </Button>
        </div>
      </div>
    </main>
  );
}
