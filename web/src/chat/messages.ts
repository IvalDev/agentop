import type { ApprovalDetails } from '../api/approvals';
import type { ChatApproval } from '../api/chat';
import { formatDate, t } from '../i18n';
import type { Messages } from '../i18n/es-ES';
import type { ChatActivity, ChatExchange } from './conversation';

/** Catalog text for the agent's current activity. */
export function activityMessage(activity: ChatActivity): string {
  return t(`chat.activity.${activity}`);
}

/** The approval actions the chat describes by name; any other tool is a generic action. */
const DESCRIBED_ACTIONS = ['send_email', 'archive_message', 'add_label'] as const;
export type ApprovalActionKey = (typeof DESCRIBED_ACTIONS)[number] | 'other';

/** Longest email body preview, in code points, before it is cut with "…". */
export const BODY_PREVIEW_MAX_CHARS = 160;

/** Which catalog entry describes `toolName`: never the internal tool name itself. */
export function approvalActionKey(toolName: string): ApprovalActionKey {
  return (DESCRIBED_ACTIONS as readonly string[]).includes(toolName) ? (toolName as ApprovalActionKey) : 'other';
}

/** What the action does, in plain words, e.g. "Enviar un correo". */
export function approvalTitle(toolName: string): string {
  return t(`chat.approvalNotice.actions.${approvalActionKey(toolName)}`);
}

/** The local short date and time of an ISO timestamp (the text as is when unparseable). */
export function approvalExpiry(expiresAt: string): string {
  return Number.isNaN(Date.parse(expiresAt))
    ? expiresAt
    : formatDate(expiresAt, { dateStyle: 'short', timeStyle: 'short' });
}

/** What the page announces for an approval the agent asked for: the action and its expiry. */
export function approvalMessage(approval: ChatApproval): string {
  return t('chat.approval', { action: approvalTitle(approval.toolName), expiresAt: approvalExpiry(approval.expiresAt) });
}

export interface ApprovalDetailRow {
  label: string;
  value: string;
}

function stringOf(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function addressesOf(value: unknown): string | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const addresses = value.filter((item): item is string => typeof item === 'string' && item.trim() !== '');
  return addresses.length > 0 ? addresses.join(', ') : null;
}

/** The body on one line, cut to `BODY_PREVIEW_MAX_CHARS` code points: enough to recognise it. */
function bodyPreview(value: unknown): string | null {
  const body = stringOf(value);
  if (body === null) {
    return null;
  }
  const characters = Array.from(body.replace(/\s+/g, ' ').trim());
  return characters.length > BODY_PREVIEW_MAX_CHARS
    ? `${characters.slice(0, BODY_PREVIEW_MAX_CHARS).join('').trimEnd()}…`
    : characters.join('');
}

type DetailField = keyof Messages['chat']['approvalNotice']['fields'];

/** Each field the action shows with its value (`null`: missing or malformed, left out). */
function detailFields(details: ApprovalDetails): Array<[DetailField, string | null]> {
  const { input } = details;
  switch (approvalActionKey(details.toolName)) {
    case 'send_email':
      return [
        ['to', addressesOf(input.to)],
        ['cc', addressesOf(input.cc)],
        ['bcc', addressesOf(input.bcc)],
        ['subject', stringOf(input.subject)],
        ['body', bodyPreview(input.body)],
      ];
    case 'archive_message':
      return [['message', stringOf(input.message_id)]];
    case 'add_label':
      return [
        ['message', stringOf(input.message_id)],
        ['label', stringOf(input.label_id)],
      ];
    case 'other':
      return [];
  }
}

/**
 * The arguments the user must see to confirm the action, labelled, in reading order: for an
 * email its recipients, subject, and a short body preview; for a message action the message
 * and label ids. Nothing else of the mailbox is shown; an unknown tool shows no arguments.
 */
export function approvalDetailRows(details: ApprovalDetails): ApprovalDetailRow[] {
  return detailFields(details).flatMap(([field, value]) =>
    value === null ? [] : [{ label: t(`chat.approvalNotice.fields.${field}`), value }],
  );
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
