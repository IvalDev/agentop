import { useEffect, useRef, useState } from 'react';
import type { ApiClient } from '../../api/client';
import {
  connectorErrorKind,
  createPairingCode,
  listConnectors,
  revokeConnector,
  type Connector,
  type PairingCode,
} from '../../api/connectors';
import { formatDate, t } from '../../i18n';
import { Alert } from '../atoms/Alert';
import { Button } from '../atoms/Button';
import { RowActionButton } from '../molecules/RowActionButton';
import { ConfirmPanel } from '../organisms/ConfirmPanel';

const TITLE_ID = 'workspace-title';
const CONFIRM_TITLE_ID = 'workspace-revoke-title';

function revokeButtonId(id: string): string {
  return `workspace-revoke-${id}`;
}

/** The local date and time of an ISO timestamp. */
function formatDateTime(value: string): string {
  return formatDate(value, { dateStyle: 'medium', timeStyle: 'short' });
}

interface Notice {
  tone: 'info' | 'error';
  text: string;
}

/**
 * Settings → Mis herramientas: the caller's local workspace permission, per user and never
 * admin-only. It generates a pairing code for the desktop widget, lists the caller's devices
 * with their connection state, and revokes one after confirming.
 *
 * The server only knows a device name and whether it is connected: this card never shows a
 * folder path, a file, or their contents (S2).
 */
export function WorkspaceCard({ api }: { api: ApiClient }) {
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [pairing, setPairing] = useState<PairingCode | null>(null);
  const [generating, setGenerating] = useState(false);
  const [revoking, setRevoking] = useState<Connector | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  // After a confirmation closes, focus returns to the row button that opened it.
  const focusTargetRef = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    listConnectors(api).then(
      (loaded) => {
        if (active) {
          setConnectors(loaded);
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

  function reload() {
    setLoading(true);
    setAttempt((value) => value + 1);
  }

  async function generate() {
    setGenerating(true);
    setNotice(null);
    try {
      setPairing(await createPairingCode(api));
    } catch {
      setNotice({ tone: 'error', text: t('myTools.workspace.errors.generate') });
    } finally {
      setGenerating(false);
    }
  }

  function remove(id: string) {
    setConnectors((current) => (current ?? []).filter((item) => item.id !== id));
  }

  async function confirmRevoke(connector: Connector) {
    setRevokeBusy(true);
    setNotice(null);
    try {
      await revokeConnector(api, connector.id);
      remove(connector.id);
      setNotice({ tone: 'info', text: t('myTools.workspace.revoke.done', { name: connector.name }) });
    } catch (error) {
      if (connectorErrorKind(error) === 'notFound') {
        // Already gone: the row is stale, and the desired state already holds.
        remove(connector.id);
        setNotice({ tone: 'info', text: t('myTools.workspace.revoke.gone', { name: connector.name }) });
      } else {
        setNotice({ tone: 'error', text: t('myTools.workspace.errors.revoke') });
      }
    } finally {
      setRevokeBusy(false);
      setRevoking(null);
      focusTargetRef.current = revokeButtonId(connector.id);
    }
  }

  const labels = {
    status: t('myTools.workspace.list.columns.status'),
    lastSeen: t('myTools.workspace.list.columns.lastSeen'),
    actions: t('myTools.workspace.list.columns.actions'),
  };

  let devices = null;
  if (connectors === null) {
    devices = loading && <p className="page__note">{t('myTools.workspace.list.loading')}</p>;
  } else if (connectors.length === 0) {
    devices = <p className="page__note">{t('myTools.workspace.list.empty')}</p>;
  } else {
    devices = (
      <table className="data-table">
        <caption className="visually-hidden">{t('myTools.workspace.list.caption')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('myTools.workspace.list.columns.name')}</th>
            <th scope="col">{labels.status}</th>
            <th scope="col">{labels.lastSeen}</th>
            <th scope="col">{labels.actions}</th>
          </tr>
        </thead>
        <tbody>
          {connectors.map((connector) => {
            const target = t('myTools.workspace.list.actionTarget', { name: connector.name });
            return (
              <tr key={connector.id}>
                <th scope="row" className="data-table__primary">
                  {connector.name}
                </th>
                <td data-label={labels.status}>
                  <span className={`status status--${connector.connected ? 'active' : 'inactive'}`}>
                    <span className="status__symbol" aria-hidden="true">
                      {connector.connected ? '●' : '○'}
                    </span>
                    {t(
                      connector.connected
                        ? 'myTools.workspace.list.connected'
                        : 'myTools.workspace.list.disconnected',
                    )}
                  </span>
                </td>
                <td data-label={labels.lastSeen}>
                  {connector.lastSeenAt === null ? (
                    t('myTools.workspace.list.neverSeen')
                  ) : (
                    <time dateTime={connector.lastSeenAt}>{formatDateTime(connector.lastSeenAt)}</time>
                  )}
                </td>
                <td data-label={labels.actions}>
                  <div className="actions">
                    <RowActionButton
                      id={revokeButtonId(connector.id)}
                      label={t('myTools.workspace.list.revoke')}
                      target={target}
                      busy={revokeBusy}
                      onClick={() => {
                        setNotice(null);
                        setRevoking(connector);
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

  let confirmation = null;
  if (revoking !== null) {
    confirmation = (
      <ConfirmPanel
        key={`revoke-${revoking.id}`}
        titleId={CONFIRM_TITLE_ID}
        title={t('myTools.workspace.revoke.title', { name: revoking.name })}
        confirmLabel={t('myTools.workspace.revoke.confirm')}
        cancelLabel={t('myTools.workspace.cancel')}
        busy={revokeBusy}
        onConfirm={() => void confirmRevoke(revoking)}
        onCancel={() => {
          setRevoking(null);
          focusTargetRef.current = revokeButtonId(revoking.id);
        }}
      >
        <p>{t('myTools.workspace.revoke.description')}</p>
      </ConfirmPanel>
    );
  }

  return (
    <section className="section" aria-labelledby={TITLE_ID}>
      <div className="section__header">
        <h2 id={TITLE_ID} className="section__title">
          {t('myTools.workspace.title')}
        </h2>
        <div className="actions">
          <Button
            aria-disabled={generating || undefined}
            onClick={generating ? undefined : generate}
          >
            {t(generating ? 'myTools.workspace.generating' : 'myTools.workspace.generate')}
          </Button>
        </div>
      </div>
      <p className="page__note">{t('myTools.workspace.description')}</p>

      {notice && <Alert tone={notice.tone}>{notice.text}</Alert>}

      {pairing !== null && (
        <Alert tone="info">
          <p>{t('myTools.workspace.pairing.title')}</p>
          <p>
            <code>{pairing.code}</code>
          </p>
          <p>{t('myTools.workspace.pairing.expires', { date: formatDateTime(pairing.expiresAt) })}</p>
          <p>{t('myTools.workspace.pairing.instructions')}</p>
        </Alert>
      )}

      {failed && (
        <>
          <Alert tone="error">{t('myTools.workspace.list.loadError')}</Alert>
          <div className="actions">
            <Button
              variant="secondary"
              aria-disabled={loading || undefined}
              onClick={loading ? undefined : reload}
            >
              {t('myTools.workspace.list.retry')}
            </Button>
          </div>
        </>
      )}

      <h3 className="section__subtitle">{t('myTools.workspace.list.title')}</h3>

      {confirmation}
      {devices}
    </section>
  );
}
