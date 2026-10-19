import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import { ApiError } from '../api/client';
import { downloadWidgetInstaller, listWidgetInstallers, type WidgetInstaller } from '../api/widgetInstallers';
import { Alert } from '../components/atoms/Alert';
import { Button } from '../components/atoms/Button';
import { usePageTitle } from '../hooks/usePageTitle';
import { formatNumber, t } from '../i18n';
import { paths } from '../routes/paths';
import { useSession } from '../session/SessionProvider';
import { detectPlatform, type Platform } from './platform';

const LIST_TITLE_ID = 'install-widget-list-title';

/** Bytes as MB with the locale's separators: 6 159 974 → "5,87 MB". */
export function formatBytes(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  return `${formatNumber(mb, { maximumFractionDigits: 2, minimumFractionDigits: 2 })} MB`;
}

/** `deb` → ".deb", `appimage` → "AppImage": how the download is offered. */
function packageLabel(installer: WidgetInstaller): string {
  return installer.packageType === 'deb' ? '.deb' : 'AppImage';
}

interface Notice {
  tone: 'info' | 'error';
  text: string;
}

/**
 * Install the desktop widget (T7): every role sees it, because the widget is just
 * another client of the same chat. The page detects the visitor's desktop and, on
 * Linux, lists the bundles the server published. A download goes through the API
 * (the session token travels in a header), so the link cannot be a plain `<a href>`.
 */
export function InstallWidgetPage() {
  const title = t('sections.installWidget.title');
  usePageTitle(title);
  const { api } = useSession();
  const platform: Platform = detectPlatform(window.navigator.userAgent);
  const [installers, setInstallers] = useState<WidgetInstaller[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    let active = true;
    listWidgetInstallers(api).then(
      (loaded) => {
        if (active) {
          setInstallers(loaded);
          setLoading(false);
          setFailed(false);
        }
      },
      () => {
        if (active) {
          setLoading(false);
          setFailed(true);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api, attempt]);

  const reload = useCallback(() => {
    setInstallers(null);
    setLoading(true);
    setAttempt((value) => value + 1);
  }, []);

  async function download(installer: WidgetInstaller) {
    setBusy(installer.filename);
    setNotice(null);
    try {
      await downloadWidgetInstaller(api, installer);
      setNotice({ tone: 'info', text: t('installWidget.downloadDone', { name: installer.filename }) });
    } catch (error) {
      // A missing file means the publication changed under us: reload the list so
      // the page stops offering something the server no longer has.
      if (error instanceof ApiError && error.status === 404) {
        reload();
      }
      setNotice({ tone: 'error', text: t('installWidget.downloadError') });
    } finally {
      setBusy(null);
    }
  }

  const isLinux = platform === 'linux';

  return (
    <div className="page install-widget">
      <h1 className="page__title">{title}</h1>
      <p className="page__lead">{t('sections.installWidget.description')}</p>

      {notice !== null && <Alert tone={notice.tone}>{notice.text}</Alert>}

      {isLinux ? (
        <section className="section" aria-labelledby={LIST_TITLE_ID}>
          <h2 id={LIST_TITLE_ID} className="section__title">
            {t('installWidget.listTitle')}
          </h2>
          {failed && (
            <>
              <Alert tone="error">{t('installWidget.loadError')}</Alert>
              <div className="actions">
                <Button variant="secondary" aria-disabled={loading || undefined} onClick={loading ? undefined : reload}>
                  {t('installWidget.retry')}
                </Button>
              </div>
            </>
          )}
          {installers === null && loading && <p className="page__note">{t('installWidget.loading')}</p>}
          {installers !== null && installers.length === 0 && (
            <p className="page__note">{t('installWidget.empty')}</p>
          )}
          {installers !== null && installers.length > 0 && (
            <table className="data-table">
              <caption className="visually-hidden">{t('installWidget.listCaption')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('installWidget.columns.package')}</th>
                  <th scope="col">{t('installWidget.columns.version')}</th>
                  <th scope="col">{t('installWidget.columns.arch')}</th>
                  <th scope="col">{t('installWidget.columns.size')}</th>
                  <th scope="col">{t('installWidget.columns.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {installers.map((installer) => (
                  <tr key={installer.filename}>
                    <th scope="row" className="data-table__primary">
                      {packageLabel(installer)}
                      <span className="data-table__note"> {installer.filename}</span>
                    </th>
                    <td data-label={t('installWidget.columns.version')}>{installer.version}</td>
                    <td data-label={t('installWidget.columns.arch')}>{installer.arch}</td>
                    <td data-label={t('installWidget.columns.size')}>{formatBytes(installer.sizeBytes)}</td>
                    <td data-label={t('installWidget.columns.actions')}>
                      <div className="actions">
                        <Button
                          aria-disabled={busy !== null || undefined}
                          onClick={busy === null ? () => void download(installer) : undefined}
                        >
                          {busy === installer.filename ? t('installWidget.downloading') : t('installWidget.download')}
                          <span className="visually-hidden">
                            {' '}
                            {t('installWidget.downloadTarget', { name: installer.filename })}
                          </span>
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {installers !== null && installers.length > 0 && (
            <div className="section__note">
              <h3 className="section__subtitle">{t('installWidget.linux.title')}</h3>
              <p>{t('installWidget.linux.intro')}</p>
              {installers.map((installer) => (
                <p key={`hint-${installer.filename}`} className="page__note">
                  {installer.packageType === 'deb'
                    ? t('installWidget.linux.debHint', { filename: installer.filename })
                    : t('installWidget.linux.appimageHint')}
                </p>
              ))}
            </div>
          )}
        </section>
      ) : (
        <section className="section">
          <h2 className="section__title">{t('installWidget.other.title')}</h2>
          <p>{t('installWidget.other.intro')}</p>
          <div className="actions">
            <Link className="button button--secondary" to={paths.chat}>
              {t('installWidget.other.backToChat')}
            </Link>
          </div>
        </section>
      )}
    </div>
  );
}
