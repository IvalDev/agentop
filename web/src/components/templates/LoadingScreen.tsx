import { t } from '../../i18n';

/** Shown while a stored session is being checked against the API. */
export function LoadingScreen() {
  return (
    <main className="auth">
      <p role="status" className="auth__loading">
        {t('app.loading')}
      </p>
    </main>
  );
}
