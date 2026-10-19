import {
  CHAT_HISTORY_LIMIT,
  CHAT_MESSAGE_MAX_CHARS,
  type ChatApproval,
  type ChatCitation,
  type ChatHistoryMessage,
  type ChatStreamEvent,
} from '../api/chat';
import { ApiError } from '../api/client';

/** What the agent is doing while no text is arriving. */
export type ChatActivity = 'thinking' | 'searching' | 'usingTool';

/** Why a turn failed; each maps to a catalog message, never to the server's text. */
export type ChatErrorKind = 'modelUnavailable' | 'network' | 'interrupted' | 'invalidMessage' | 'unexpected';

export type ExchangeStatus = 'streaming' | 'completed' | 'stepLimit' | 'failed' | 'stopped';

/** One question and the agent's reply to it. */
export interface ChatExchange {
  id: number;
  question: string;
  /** Streamed text until `done`, then `done.answer`. */
  answer: string;
  status: ExchangeStatus;
  activity: ChatActivity | null;
  citations: ChatCitation[];
  approvals: ChatApproval[];
  error: ChatErrorKind | null;
}

export type ConversationAction =
  | { type: 'send'; id: number; question: string }
  | { type: 'event'; id: number; event: ChatStreamEvent }
  | { type: 'fail'; id: number; error: ChatErrorKind }
  /** The stream ended: without `done` or `error`, the reply was cut short. */
  | { type: 'end'; id: number }
  | { type: 'stop'; id: number }
  | { type: 'reset' };

/** The tool that searches the knowledge base (`app/modules/tools/builtin.py`). */
const SEARCH_TOOL = 'search_documents';

function mergeApprovals(current: ChatApproval[], incoming: ChatApproval[]): ChatApproval[] {
  const known = new Set(current.map((approval) => approval.approvalId));
  return [...current, ...incoming.filter((approval) => !known.has(approval.approvalId))];
}

/** The stream's `error` messages are fixed English strings: map them, never show them. */
function streamErrorKind(message: string): ChatErrorKind {
  return message === 'language model unavailable' ? 'modelUnavailable' : 'unexpected';
}

function applyEvent(exchange: ChatExchange, event: ChatStreamEvent): ChatExchange {
  switch (event.type) {
    case 'run_started':
      return exchange;
    case 'token':
      return { ...exchange, answer: exchange.answer + event.text, activity: null };
    case 'tool_call':
      return { ...exchange, activity: event.toolName === SEARCH_TOOL ? 'searching' : 'usingTool' };
    case 'tool_result':
      return { ...exchange, activity: 'thinking' };
    case 'approval_required':
      return { ...exchange, approvals: mergeApprovals(exchange.approvals, [event.approval]) };
    case 'citations':
      return { ...exchange, citations: event.citations };
    case 'done':
      return {
        ...exchange,
        answer: event.answer,
        citations: event.citations,
        approvals: mergeApprovals(exchange.approvals, event.approvals),
        status: event.stopReason === 'step_limit' ? 'stepLimit' : 'completed',
        activity: null,
      };
    case 'error':
      return { ...exchange, status: 'failed', error: streamErrorKind(event.message), activity: null };
  }
}

function settle(exchange: ChatExchange, action: ConversationAction): ChatExchange {
  switch (action.type) {
    case 'event':
      return applyEvent(exchange, action.event);
    case 'fail':
      return { ...exchange, status: 'failed', error: action.error, activity: null };
    case 'end':
      return { ...exchange, status: 'failed', error: 'interrupted', activity: null };
    case 'stop':
      return { ...exchange, status: 'stopped', activity: null };
    default:
      return exchange;
  }
}

export function conversationReducer(exchanges: ChatExchange[], action: ConversationAction): ChatExchange[] {
  switch (action.type) {
    case 'send':
      return [
        ...exchanges,
        {
          id: action.id,
          question: action.question,
          answer: '',
          status: 'streaming',
          activity: 'thinking',
          citations: [],
          approvals: [],
          error: null,
        },
      ];
    case 'reset':
      return [];
    default:
      // Only a streaming turn changes: late events after Detener or a failure are ignored.
      return exchanges.map((exchange) =>
        exchange.id === action.id && exchange.status === 'streaming' ? settle(exchange, action) : exchange,
      );
  }
}

/** Cut to `max` code points (the API counts characters, not UTF-16 units). */
function clip(text: string, max: number): string {
  const characters = Array.from(text);
  return characters.length > max ? characters.slice(0, max).join('') : text;
}

/**
 * The history replayed with the next message, as the dev console builds it: every turn that
 * reached `done` with an answer, assistant text capped, the last `CHAT_HISTORY_LIMIT` messages.
 */
export function buildHistory(exchanges: ChatExchange[]): ChatHistoryMessage[] {
  const messages = exchanges.flatMap((exchange): ChatHistoryMessage[] =>
    (exchange.status === 'completed' || exchange.status === 'stepLimit') && exchange.answer
      ? [
          { role: 'user', content: exchange.question },
          { role: 'assistant', content: clip(exchange.answer, CHAT_MESSAGE_MAX_CHARS) },
        ]
      : [],
  );
  return messages.slice(-CHAT_HISTORY_LIMIT);
}

/** Why sending failed before the stream started. */
export function requestErrorKind(error: unknown): ChatErrorKind {
  if (error instanceof ApiError) {
    if (error.status === 0 || (error.status >= 502 && error.status <= 504)) {
      return 'network';
    }
    if (error.status === 422) {
      return 'invalidMessage';
    }
    return 'unexpected';
  }
  // The body failed mid-read (connection dropped).
  return error instanceof TypeError ? 'interrupted' : 'unexpected';
}
