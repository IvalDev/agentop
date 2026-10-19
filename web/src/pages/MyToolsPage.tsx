import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { listModules, type CatalogModule } from '../api/modules';
import { getOnboardingState } from '../api/onboarding';
import type { ApiClient } from '../api/client';
import { Alert } from '../components/atoms/Alert';
import { Button } from '../components/atoms/Button';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';
import { paths } from '../routes/paths';
import { useSession } from '../session/SessionProvider';

const LIST_TITLE_ID = 'my-tools-list-title';

/** How one active module's own onboarding is doing; `error` when the check failed. */
type ModuleStatus = { kind: 'connected' } | { kind: 'pending' } | { kind: 'error' };

async function moduleStatus(api: ApiClient, key: string): Promise<ModuleStatus> {
  try {
    const state = await getOnboardingState(api, key);
    return state.status === 'completed' ? { kind: 'connected' } : { kind: 'pending' };
  } catch {
    return { kind: 'error' };
  }
}

/**
 * Settings → Mis herramientas (every role): the tenant's ACTIVE modules with the
 * caller's own connection status and a CTA into the onboarding wizard. Activation
 * itself stays in the admin Tools page; this page only configures the own account.
 */
export function MyToolsPage() {
  const title = t('sections.myTools.title');
  usePageTitle(title);
  const { api } = useSession();
  const [modules, setModules] = useState<CatalogModule[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [statuses, setStatuses] = useState<Record<string, ModuleStatus> | null>(null);

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

  // The own status of every active module, checked in parallel; a failed check
  // only marks that row, it never hides the module. The result replaces the
  // whole map, so a stale entry can only show "checking" until it lands.
  useEffect(() => {
    if (modules === null) {
      return;
    }
    const activeModules = modules.filter((module) => module.active);
    if (activeModules.length === 0) {
      return;
    }
    let current = true;
    Promise.all(activeModules.map(async (module) => [module.key, await moduleStatus(api, module.key)] as const)).then(
      (entries) => {
        if (current) {
          setStatuses(Object.fromEntries(entries));
        }
      },
    );
    return () => {
      current = false;
    };
  }, [api, modules]);

  function reload() {
    setLoading(true);
    setStatuses(null);
    setAttempt((value) => value + 1);
  }

  const activeModules = (modules ?? []).filter((module) => module.active);

  let body = null;
  if (modules === null) {
    body = loading && <p className="page__note">{t('myTools.list.loading')}</p>;
  } else if (activeModules.length === 0) {
    body = <p className="page__note">{t('myTools.list.empty')}</p>;
  } else {
    const labels = {
      description: t('myTools.list.columns.description'),
      status: t('myTools.list.columns.status'),
      actions: t('myTools.list.columns.actions'),
    };
    body = (
      <table className="data-table">
        <caption className="visually-hidden">{t('myTools.list.caption')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('myTools.list.columns.module')}</th>
            <th scope="col">{labels.description}</th>
            <th scope="col">{labels.status}</th>
            <th scope="col">{labels.actions}</th>
          </tr>
        </thead>
        <tbody>
          {activeModules.map((module) => {
            const status = statuses?.[module.key];
            return (
              <tr key={module.key}>
                <th scope="row" className="data-table__primary">
                  {module.name}
                </th>
                <td data-label={labels.description} className="data-table__primary">
                  {module.description}
                </td>
                <td data-label={labels.status}>
                  {status === undefined && t('myTools.list.checking')}
                  {status?.kind === 'connected' && (
                    <span className="status status--active">
                      <span className="status__symbol" aria-hidden="true">
                        ●
                      </span>
                      {t('myTools.list.connected')}
                    </span>
                  )}
                  {status?.kind === 'pending' && (
                    <span className="status status--inactive">
                      <span className="status__symbol" aria-hidden="true">
                        ○
                      </span>
                      {t('myTools.list.pending')}
                    </span>
                  )}
                  {status?.kind === 'error' && <span className="data-table__note">{t('myTools.list.statusError')}</span>}
                </td>
                <td data-label={labels.actions}>
                  {status?.kind === 'pending' && (
                    <div className="actions">
                      <Link className="button button--primary" to={paths.onboarding(module.key)}>
                        {t('myTools.list.configure')}{' '}
                        <span className="visually-hidden">{t('myTools.list.actionTarget', { name: module.name })}</span>
                      </Link>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    );
  }

  return (
    <div className="page my-tools">
      <h1 className="page__title">{title}</h1>
      <p className="page__lead">{t('sections.myTools.description')}</p>
      <section className="section" aria-labelledby={LIST_TITLE_ID}>
        <h2 id={LIST_TITLE_ID} className="section__title">
          {t('myTools.list.title')}
        </h2>
        {failed && (
          <>
            <Alert tone="error">{t('myTools.list.loadError')}</Alert>
            <div className="actions">
              <Button
                variant="secondary"
                aria-disabled={loading || undefined}
                onClick={loading ? undefined : reload}
              >
                {t('myTools.list.retry')}
              </Button>
            </div>
          </>
        )}
        {body}
      </section>
    </div>
  );
}
