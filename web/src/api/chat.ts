import type { ApiClient } from './client';

/**
 * `POST /api/v1/chat/stream`: the agent's run as Server-Sent Events.
 *
 * Wire format (`app/modules/agent/router.py`): `event: <type>\ndata: <json>\n\n` with the
 * types `run_started`, `token`, `tool_call`, `tool_result`, `approval_required`,
 * `citations`, `done` (the full `ChatResponse`; its `answer` is authoritative), and `error`.
 * Everything here is untrusted text: the UI renders it as text, never as HTML.
 */

/** Prior messages replayed with each turn, as the dev console and `app/demo/chat.py`. */
export const CHAT_HISTORY_LIMIT = 10;
/** `CHAT_MESSAGE_MAX_CHARS` in `packages/contracts/schemas.py`, counted in code points. */
export const CHAT_MESSAGE_MAX_CHARS = 4000;

export interface ChatHistoryMessage {
  role: 'user' | 'assistant';
  content: string;
}

/** A numbered passage the agent read; `number` is the `[n]` marker in the answer. */
export interface ChatCitation {
  number: number;
  documentFilename: string;
  content: string;
}

/** An action the agent submitted for human approval (not executed). */
export interface ChatApproval {
  approvalId: string;
  toolName: string;
  expiresAt: string;
}

export type ChatStreamEvent =
  | { type: 'run_started' }
  | { type: 'token'; text: string }
  | { type: 'tool_call'; toolName: string }
  | { type: 'tool_result'; toolName: string }
  | { type: 'approval_required'; approval: ChatApproval }
  | { type: 'citations'; citations: ChatCitation[] }
  | {
      type: 'done';
      answer: string;
      stopReason: 'completed' | 'step_limit';
      citations: ChatCitation[];
      approvals: ChatApproval[];
    }
  | { type: 'error'; message: string };

/** One raw SSE frame: its `event:` name and its joined `data:` lines. */
export interface ServerSentEvent {
  type: string;
  data: string;
}

/**
 * Parse a Server-Sent Events body (`EventSource` cannot POST). Chunks may end mid-line or
 * mid-character: the decoder keeps partial UTF-8 sequences and the buffer keeps the
 * unfinished line for the next read.
 */
export async function* readServerSentEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ServerSentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let type = 'message';
  let data: string[] = [];
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      if (done) {
        lines.push(buffer, ''); // flush the tail and end the last event
      }
      for (const raw of lines) {
        const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
        if (line === '') {
          if (data.length > 0) {
            yield { type, data: data.join('\n') };
          }
          type = 'message';
          data = [];
        } else if (line.startsWith('event:')) {
          type = line.slice('event:'.length).trim();
        } else if (line.startsWith('data:')) {
          data.push(line.slice('data:'.length).replace(/^ /, ''));
        }
      }
      if (done) {
        return;
      }
    }
  } finally {
    // Stops the download when the reader leaves early (Detener, new conversation).
    reader.cancel().catch(() => undefined);
  }
}

type Payload = Record<string, unknown>;

function isPayload(value: unknown): value is Payload {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function parseCitations(value: unknown): ChatCitation[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isPayload).flatMap((item) =>
    typeof item.number === 'number'
      ? [{ number: item.number, documentFilename: text(item.document_filename), content: text(item.content) }]
      : [],
  );
}

function parseApproval(value: unknown): ChatApproval | null {
  if (!isPayload(value) || typeof value.approval_id !== 'string') {
    return null;
  }
  return { approvalId: value.approval_id, toolName: text(value.tool_name), expiresAt: text(value.expires_at) };
}

/** Type one frame; `null` for unknown types or malformed data (skipped, never shown). */
export function parseChatEvent({ type, data }: ServerSentEvent): ChatStreamEvent | null {
  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return null;
  }
  if (!isPayload(payload)) {
    return null;
  }
  switch (type) {
    case 'run_started':
      return { type };
    case 'token':
      return { type, text: text(payload.text) };
    case 'tool_call':
    case 'tool_result':
      return { type, toolName: text(payload.tool_name) };
    case 'approval_required': {
      const approval = parseApproval(payload);
      return approval && { type, approval };
    }
    case 'citations':
      return { type, citations: parseCitations(payload.citations) };
    case 'done':
      return {
        type,
        answer: text(payload.answer),
        stopReason: payload.stop_reason === 'step_limit' ? 'step_limit' : 'completed',
        citations: parseCitations(payload.citations),
        approvals: Array.isArray(payload.pending_approvals)
          ? payload.pending_approvals.flatMap((item) => parseApproval(item) ?? [])
          : [],
      };
    case 'error':
      return { type, message: text(payload.message) };
    default:
      return null;
  }
}

/** Send one message with its history and yield the run's events until the stream ends. */
export async function* streamChat(
  api: ApiClient,
  message: string,
  history: ChatHistoryMessage[],
  signal: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const response = await api.send('/api/v1/chat/stream', {
    method: 'POST',
    body: { message, history },
    headers: { Accept: 'text/event-stream' },
    signal,
  });
  if (response.body === null) {
    return;
  }
  for await (const frame of readServerSentEvents(response.body)) {
    const event = parseChatEvent(frame);
    if (event !== null) {
      yield event;
    }
  }
}
