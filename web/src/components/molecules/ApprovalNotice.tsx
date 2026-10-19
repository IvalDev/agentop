import { useEffect, useRef, useState } from 'react';
import {
  approvalErrorKind,
  approveApproval,
  executeApproval,
  getApproval,
  precheckReason,
  rejectApproval,
  type ApprovalDetails,
  type ApprovalErrorKind,
} from '../../api/approvals';
import type { ChatApproval } from '../../api/chat';
import {
  approvalActionKey,
  approvalDetailRows,
  approvalExpiry,
  approvalTitle,
} from '../../chat/messages';
import { useAnnouncer, type Announcement } from '../../hooks/useAnnouncer';
import { t } from '../../i18n';
import { useSession } from '../../session/SessionProvider';
import { Announcer } from '../atoms/Announcer';
import { Button } from '../atoms/Button';

/** A call in flight: reading the request, or one of the user's decisions. */
export type ApprovalBusy = 'loading' | 'approving' | 'executing' | 'rejecting';

export interface ApprovalNoticeState {
  /** The stored request, once read; `null` while loading or when it could not be read. */
  details: ApprovalDetails | null;
  busy: ApprovalBusy | null;
  /** Why the last call failed, until the next one starts. */
  error: ApprovalErrorKind | null;
}

export interface ApprovalNoticeViewProps {
  /** What the agent asked for, from the chat stream (always known, even before loading). */
  approval: ChatApproval;
  state: ApprovalNoticeState;
  announcement: Announcement;
  /** Approve a pending request and execute it, or execute an already approved one. */
  onConfirm: () => void;
  onReject: () => void;
  /** Read the request again after it could not be loaded. */
  onRetry: () => void;
}

interface StatusLine {
  text: string;
  tone: 'info' | 'success' | 'error';
}

function errorLine(kind: ApprovalErrorKind): StatusLine {
  return {
    text: kind === 'expired' ? t('chat.approvalNotice.status.expired') : t(`chat.approvalNotice.errors.${kind}`),
    tone: kind === 'expired' ? 'info' : 'error',
  };
}

/** What the request's own state says, `null` while it can still be decided. */
function outcomeLine(details: ApprovalDetails): StatusLine | null {
  switch (details.status) {
    case 'executed':
      return { text: t(`chat.approvalNotice.done.${approvalActionKey(details.toolName)}`), tone: 'success' };
    case 'failed': {
      const reason = precheckReason(details.executionError);
      return {
        text:
          reason === null
            ? t('chat.approvalNotice.status.failed')
            : t('chat.approvalNotice.status.failedWithReason', { reason }),
        tone: 'error',
      };
    }
    case 'rejected':
    case 'expired':
    case 'executing':
      return { text: t(`chat.approvalNotice.status.${details.status}`), tone: 'info' };
    default:
      return null;
  }
}

/** Errors after which deciding again cannot help: the request is gone or not the user's. */
const FINAL_ERRORS: readonly ApprovalErrorKind[] = ['forbidden', 'notFound', 'expired'];

/**
 * What the notice says now, and whether the user may decide: only with the details on
 * screen (they must see what will happen), no call in flight, and a pending or approved
 * request that no final error closed.
 */
export function describeNotice(state: ApprovalNoticeState): { status: StatusLine | null; decidable: boolean } {
  const { details, busy, error } = state;
  if (busy !== null) {
    return { status: { text: t(`chat.approvalNotice.progress.${busy}`), tone: 'info' }, decidable: false };
  }
  if (details === null) {
    return { status: error === null ? null : errorLine(error), decidable: false };
  }
  if (error !== null && FINAL_ERRORS.includes(error)) {
    return { status: errorLine(error), decidable: false };
  }
  const outcome = outcomeLine(details);
  if (outcome !== null) {
    return { status: outcome, decidable: false };
  }
  return { status: error === null ? null : errorLine(error), decidable: true };
}

/**
 * The confirmation of an action the agent asked for: what will happen in plain words (the
 * action and the arguments the user must check, nothing else), then "Aprobar y ejecutar" and
 * "Rechazar". The buttons stay focusable while a call runs and after the outcome, but do
 * nothing (`aria-disabled`); progress and outcome are shown and announced politely.
 */
export function ApprovalNoticeView({ approval, state, announcement, onConfirm, onReject, onRetry }: ApprovalNoticeViewProps) {
  const titleId = `chat-approval-${approval.approvalId}-title`;
  const { status, decidable } = describeNotice(state);
  const { details, busy, error } = state;
  const rows = details === null ? [] : approvalDetailRows(details);
  const canRetry = details === null && busy === null && (error === 'network' || error === 'unexpected');

  return (
    <div role="group" aria-labelledby={titleId} className="chat-approval">
      <h3 id={titleId} className="chat-approval__title">
        {approvalTitle(approval.toolName)}
      </h3>
      <p>{t('chat.approvalNotice.intro', { expiresAt: approvalExpiry(details?.expiresAt ?? approval.expiresAt) })}</p>
      {rows.length > 0 && (
        <dl className="chat-approval__details">
          {rows.map((row) => (
            <div key={row.label} className="chat-approval__detail">
              <dt>{row.label}</dt>
              <dd>{row.value}</dd>
            </div>
          ))}
        </dl>
      )}
      <p className="chat-approval__reference">{t('chat.approvalNotice.reference', { id: approval.approvalId })}</p>
      {status && <p className={`chat-approval__status chat-approval__status--${status.tone}`}>{status.text}</p>}
      {details !== null && (
        <div className="actions">
          <Button aria-disabled={!decidable || undefined} onClick={decidable ? onConfirm : undefined}>
            {t('chat.approvalNotice.confirm')}
          </Button>
          <Button variant="secondary" aria-disabled={!decidable || undefined} onClick={decidable ? onReject : undefined}>
            {t('chat.approvalNotice.reject')}
          </Button>
        </div>
      )}
      {canRetry && (
        <div className="actions">
          <Button variant="secondary" onClick={onRetry}>
            {t('chat.approvalNotice.retry')}
          </Button>
        </div>
      )}
      <Announcer announcement={announcement} />
    </div>
  );
}

const LOADING: ApprovalNoticeState = { details: null, busy: 'loading', error: null };

/**
 * Container of {@link ApprovalNoticeView}: reads the stored request (the server is the source
 * of truth, so a notice shown again after visiting another section shows its real state), and
 * runs the decisions through the approvals API.
 */
export function ApprovalNotice({ approval }: { approval: ChatApproval }) {
  const { api } = useSession();
  const { approvalId } = approval;
  const [state, setState] = useState<ApprovalNoticeState>(LOADING);
  const [attempt, setAttempt] = useState(0);
  const [announcement, announce] = useAnnouncer();
  // Decisions are never aborted (the server may already act on them): late replies are dropped.
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    getApproval(api, approvalId, controller.signal).then(
      (details) => {
        if (!controller.signal.aborted) {
          setState({ details, busy: null, error: null });
        }
      },
      (error: unknown) => {
        if (!controller.signal.aborted) {
          setState({ details: null, busy: null, error: approvalErrorKind(error) });
        }
      },
    );
    return () => controller.abort();
  }, [api, approvalId, attempt]);

  /** Show `next` and announce what it says, naming the action (several may be on screen). */
  function update(next: ApprovalNoticeState) {
    if (!mountedRef.current) {
      return;
    }
    setState(next);
    const { status } = describeNotice(next);
    if (status !== null) {
      announce(t('chat.approvalNotice.announcement', { action: approvalTitle(approval.toolName), status: status.text }));
    }
  }

  /** After a refused call. A conflict means it was decided elsewhere: show its real state. */
  async function fail(details: ApprovalDetails, error: unknown) {
    const kind = approvalErrorKind(error);
    if (kind === 'conflict') {
      try {
        update({ details: await getApproval(api, approvalId), busy: null, error: null });
        return;
      } catch {
        // Keep the conflict message with the last known details.
      }
    }
    update({ details, busy: null, error: kind });
  }

  async function confirm() {
    let { details } = state;
    if (details === null || state.busy !== null) {
      return;
    }
    try {
      if (details.status === 'pending') {
        update({ details, busy: 'approving', error: null });
        details = await approveApproval(api, approvalId);
      }
      if (details.status === 'approved') {
        update({ details, busy: 'executing', error: null });
        details = await executeApproval(api, approvalId);
      }
      update({ details, busy: null, error: null });
    } catch (error) {
      await fail(details, error);
    }
  }

  async function reject() {
    const { details } = state;
    if (details === null || state.busy !== null) {
      return;
    }
    try {
      update({ details, busy: 'rejecting', error: null });
      update({ details: await rejectApproval(api, approvalId), busy: null, error: null });
    } catch (error) {
      await fail(details, error);
    }
  }

  return (
    <ApprovalNoticeView
      approval={approval}
      state={state}
      announcement={announcement}
      onConfirm={() => void confirm()}
      onReject={() => void reject()}
      onRetry={() => {
        setState(LOADING);
        setAttempt((current) => current + 1);
      }}
    />
  );
}
