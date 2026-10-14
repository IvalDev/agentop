import type { ChatApproval } from '../api/chat';
import { formatDate, t } from '../i18n';
import type { ChatActivity, ChatExchange } from './conversation';

/** Catalog text for the agent's current activity. */
export function activityMessage(activity: ChatActivity): string {
  return t(`chat.activity.${activity}`);
}

/** The approval notice, with its id and local expiry time. */
export function approvalMessage(approval: ChatApproval): string {
  const expiresAt = Number.isNaN(Date.parse(approval.expiresAt))
    ? approval.expiresAt
    : formatDate(approval.expiresAt, { dateStyle: 'short', timeStyle: 'short' });
  return t('chat.approval', { id: approval.approvalId, expiresAt });
}

/** How a turn ended when it did not end with a normal answer: stopped, step limit, or error. */
export function statusMessage(exchange: ChatExchange): { text: string; tone: 'info' | 'error' } | null {
  switch (exchange.status) {
    case 'stepLimit':
      return { text: t('chat.notices.stepLimit'), tone: 'info' };
    case 'stopped':
      return { text: t('chat.notices.stopped'), tone: 'info' };
    case 'failed':
      return { text: t(`chat.errors.${exchange.error ?? 'unexpected'}`), tone: 'error' };
    default:
      return null;
  }
}

/**
 * What the polite live region says about the latest turn: the activity while it works,
 * then the complete answer once (never each token), or how it ended.
 */
export function announcementFor(exchange: ChatExchange): string {
  if (exchange.status === 'streaming') {
    return exchange.activity ? activityMessage(exchange.activity) : '';
  }
  const status = statusMessage(exchange);
  if (status !== null) {
    return status.text;
  }
  const answer = exchange.answer ? [t('chat.answerAnnouncement', { answer: exchange.answer })] : [];
  return [...answer, ...exchange.approvals.map(approvalMessage)].join(' ');
}
