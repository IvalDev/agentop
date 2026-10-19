import { useCallback, useEffect, useRef, useState } from 'react';
import { activateModule, deactivateModule, listModules, type CatalogModule } from '../api/modules';
import { ApiError } from '../api/client';
import { Alert } from '../components/atoms/Alert';
import { Button } from '../components/atoms/Button';
import { RowActionButton } from '../components/molecules/RowActionButton';
import { ConfirmPanel } from '../components/organisms/ConfirmPanel';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';
import { useSession } from '../session/SessionProvider';

const LIST_TITLE_ID = 'tools-list-title';
const CONFIRM_TITLE_ID = 'tools-confirm-title';

/** The confirmation step waiting for an answer. */
type PendingChange = { kind: 'activate'; module: CatalogModule } | { kind: 'deactivate'; module: CatalogModule };

interface Notice {
  tone: 'info' | 'error';
  text: string;
}

/** DOM id of a module's action button, so focus can return to it after a confirmation step. */
function moduleActionId(key: string): string {
  return `module-${key}-state`;
}

/** Why a module call failed; each maps to a catalog message, never to the server's text. */
type ModuleErrorKind = 'forbidden' | 'notFound' | 'network' | 'unexpected';

function moduleErrorKind(error: unknown): ModuleErrorKind {
  if (!(error instanceof ApiError)) {
    return 'unexpected';
  }
  switch (error.status) {
    case 0:
    case 502:
    case 503:
    case 504:
      return 'network';
    case 403:
      return 'forbidden';
    case 404:
      return 'notFound';
    default:
      return 'unexpected';
  }
}

/**
 * Settings → Tools (owner and admin): the tenant's activation switches over the module catalog.
 * Every change asks for confirmation, applies only the state the API answers with, and leaves
 * the rest of the list untouched; the API enforces the role rule anyway.
 */
export function ToolsPage() {
  const title = t('sections.tools.title');
  usePageTitle(title);
  const { api } = useSession();
  const [modules, setModules] = useState<CatalogModule[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [pending, setPending] = useState<PendingChange | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  // After a confirmation closes, focus returns to the row button that opened it.
  const focusTargetRef = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    listModules(api).then(
      (loaded) => {
        if (active) {
          setModules(loaded);
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

  useEffect(() => {
    const id = focusTargetRef.current;
    if (id !== null) {
      focusTargetRef.current = null;
      document.getElementById(id)?.focus();
    }
  });

  const reload = useCallback(() => {
    setLoading(true);
    setAttempt((current) => current + 1);
  }, []);

  function close(module: CatalogModule) {
    setPending(null);
    focusTargetRef.current = moduleActionId(module.key);
  }

  async function apply(module: CatalogModule, kind: 'activate' | 'deactivate') {
    setBusyKey(module.key);
    setNotice(null);
    try {
      const updated =
        kind === 'activate' ? await activateModule(api, module.key) : await deactivateModule(api, module.key);
      // Only the server's answer moves the switch; nothing is optimistic.
      setModules((current) =>
        (current ?? []).map((item) => (item.key === updated.key ? { ...item, active: updated.active } : item)),
      );
      setNotice({
        tone: 'info',
        text: t(kind === 'activate' ? 'tools.activation.done' : 'tools.deactivation.done', { name: module.name }),
      });
    } catch (error) {
      const kindFailed = moduleErrorKind(error);
      setNotice({ tone: 'error', text: t(`tools.errors.${kindFailed}`) });
      if (kindFailed === 'notFound') {
        reload();
      }
    } finally {
      setBusyKey(null);
      close(module);
    }
  }

  let confirmation = null;
  if (pending?.kind === 'activate') {
    const { module } = pending;
    confirmation = (
      <ConfirmPanel
        key={`activate-${module.key}`}
        titleId={CONFIRM_TITLE_ID}
        title={t('tools.activation.title', { name: module.name })}
        confirmLabel={t('tools.activation.confirm')}
        cancelLabel={t('tools.cancel')}
        busy={busyKey !== null}
        onConfirm={() => void apply(module, 'activate')}
        onCancel={() => close(module)}
      >
        <p>{t('tools.activation.description')}</p>
      </ConfirmPanel>
    );
  } else if (pending?.kind === 'deactivate') {
    const { module } = pending;
    confirmation = (
      <ConfirmPanel
        key={`deactivate-${module.key}`}
        titleId={CONFIRM_TITLE_ID}
        title={t('tools.deactivation.title', { name: module.name })}
        confirmLabel={t('tools.deactivation.confirm')}
        cancelLabel={t('tools.cancel')}
        busy={busyKey !== null}
        onConfirm={() => void apply(module, 'deactivate')}
        onCancel={() => close(module)}
      >
        <p>{t('tools.deactivation.description')}</p>
      </ConfirmPanel>
    );
  }

  let body = null;
  if (modules === null) {
    body = loading && <p className="page__note">{t('tools.list.loading')}</p>;
  } else if (modules.length === 0) {
    body = <p className="page__note">{t('tools.list.empty')}</p>;
  } else {
    const labels = {
      description: t('tools.list.columns.description'),
      version: t('tools.list.columns.version'),
      state: t('tools.list.columns.state'),
      actions: t('tools.list.columns.actions'),
    };
    body = (
      <table className="data-table">
        <caption className="visually-hidden">{t('tools.list.caption')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('tools.list.columns.module')}</th>
            <th scope="col">{labels.description}</th>
            <th scope="col">{labels.version}</th>
            <th scope="col">{labels.state}</th>
            <th scope="col">{labels.actions}</th>
          </tr>
        </thead>
        <tbody>
          {modules.map((module) => {
            const target = t('tools.list.actionTarget', { name: module.name });
            const busy = busyKey === module.key;
            return (
              <tr key={module.key}>
                <th scope="row" className="data-table__primary">
                  {module.name}
                </th>
                <td data-label={labels.description} className="data-table__primary">
                  {module.description}
                  {module.tools.length > 0 && (
                    <p className="data-table__note">{t('tools.list.toolsNote', { list: module.tools.join(', ') })}</p>
                  )}
                  {module.permissions.length > 0 && (
                    <p className="data-table__note">
                      {t('tools.list.permissionsNote', { list: module.permissions.join(', ') })}
                    </p>
                  )}
                </td>
                <td data-label={labels.version}>{module.version}</td>
                <td data-label={labels.state}>
                  <span className={`status status--${module.active ? 'active' : 'inactive'}`}>
                    <span className="status__symbol" aria-hidden="true">
                      {module.active ? '●' : '○'}
                    </span>
                    {t(module.active ? 'tools.list.active' : 'tools.list.inactive')}
                  </span>
                </td>
                <td data-label={labels.actions}>
                  <div className="actions">
                    <RowActionButton
                      id={moduleActionId(module.key)}
                      label={t(module.active ? 'tools.list.deactivate' : 'tools.list.activate')}
                      target={target}
                      busy={busy}
                      onClick={() => {
                        setNotice(null);
                        setPending({ kind: module.active ? 'deactivate' : 'activate', module });
                      }}
                    />
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    );
  }

  return (
    <div className="page tools">
      <h1 className="page__title">{title}</h1>
      <p className="page__lead">{t('sections.tools.description')}</p>
      <section className="section" aria-labelledby={LIST_TITLE_ID}>
        <h2 id={LIST_TITLE_ID} className="section__title">
          {t('tools.list.title')}
        </h2>
        {notice && <Alert tone={notice.tone}>{notice.text}</Alert>}
        {failed && (
          <>
            <Alert tone="error">{t('tools.list.loadError')}</Alert>
            <div className="actions">
              <Button
                variant="secondary"
                aria-disabled={loading || undefined}
                onClick={loading ? undefined : reload}
              >
                {t('tools.list.retry')}
              </Button>
            </div>
          </>
        )}
        {confirmation}
        {body}
      </section>
    </div>
  );
}
