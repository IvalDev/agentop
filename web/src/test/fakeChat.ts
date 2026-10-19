import { vi } from 'vitest';
import type { ChatHistoryMessage } from '../api/chat';
import type { Role } from '../session/roles';
import { jsonResponse, meBody, type FakeReply, type FakeRoutes } from './fakeApi';

/** One SSE frame as `app/modules/agent/router.py` writes it. */
export function sseFrame(type: string, data: unknown): string {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** A `done` frame: the full `ChatResponse`, completed and without citations unless overridden. */
export function doneFrame(answer: string, overrides: Record<string, unknown> = {}): string {
  return sseFrame('done', {
    run_id: 'run-1',
    answer,
    stop_reason: 'completed',
    citations: [],
    sources: [],
    pending_approvals: [],
    tool_calls: [],
    ...overrides,
  });
}

/** UTF-8 bytes of `text` cut every `size` bytes: lines and multi-byte characters get split. */
export function chunkBytes(text: string, size: number): Uint8Array[] {
  const bytes = new TextEncoder().encode(text);
  const chunks: Uint8Array[] = [];
  for (let start = 0; start < bytes.length; start += size) {
    chunks.push(bytes.slice(start, start + size));
  }
  return chunks;
}

/** A response body the test feeds by hand; aborting the request errors it like `fetch` does. */
export class ControlledStream {
  readonly body: ReadableStream<Uint8Array>;
  private controller!: ReadableStreamDefaultController<Uint8Array>;
  private finished = false;

  constructor() {
    this.body = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
    });
  }

  /** A body holding `frames`, cut every `chunkSize` bytes, already closed. */
  static of(frames: string, chunkSize = 5): ControlledStream {
    const stream = new ControlledStream();
    stream.push(frames, chunkSize);
    stream.close();
    return stream;
  }

  push(text: string, chunkSize = 5): void {
    for (const chunk of chunkBytes(text, chunkSize)) {
      this.controller.enqueue(chunk);
    }
  }

  close(): void {
    if (!this.finished) {
      this.finished = true;
      this.controller.close();
    }
  }

  bindAbort(signal: AbortSignal | null | undefined): void {
    signal?.addEventListener('abort', () => {
      if (!this.finished) {
        this.finished = true;
        this.controller.error(new DOMException('The operation was aborted.', 'AbortError'));
      }
    });
  }
}

/** A chat reply: SSE frames (sent at once), a stream fed by the test, or an HTTP error. */
export type ChatReply = string | ControlledStream | FakeReply;

export interface RecordedChatRequest {
  message: string;
  history: ChatHistoryMessage[];
  /** The browser's IANA time zone; absent when `streamChat` could not resolve one. */
  timezone?: string;
  signal: AbortSignal | null | undefined;
}

/**
 * Fake API for a signed-in `role` whose `POST /api/v1/chat/stream` answers `replies` in
 * order, and whose other `routes` (e.g. the approvals API) answer like `installFakeApi`.
 * Returns the recorded request bodies (and their abort signals).
 */
export function installChatApi(role: Role, replies: ChatReply[], routes: FakeRoutes = {}) {
  const requests: RecordedChatRequest[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const route = `${(init.method ?? 'GET').toUpperCase()} ${String(input)}`;
    if (route === 'GET /api/v1/auth/me') {
      return jsonResponse({ status: 200, body: meBody(role) });
    }
    const handler = routes[route];
    if (handler !== undefined) {
      return jsonResponse(typeof handler === 'function' ? await handler(init) : handler);
    }
    if (route !== 'POST /api/v1/chat/stream') {
      return jsonResponse({ status: 404, body: { detail: 'Not Found' } });
    }
    const body = JSON.parse(String(init.body)) as Omit<RecordedChatRequest, 'signal'>;
    requests.push({ ...body, signal: init.signal });
    const reply = replies.shift() ?? { status: 500, body: { detail: 'no scripted reply' } };
    if (typeof reply === 'object' && !(reply instanceof ControlledStream)) {
      return jsonResponse(reply);
    }
    const stream = typeof reply === 'string' ? ControlledStream.of(reply) : reply;
    stream.bindAbort(init.signal);
    return new Response(stream.body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, requests };
}

/** An `ApprovalResponse` (`GET /api/v1/approvals/{id}`) for `approvalId`, pending by default. */
export function approvalBody(approvalId: string, overrides: Record<string, unknown> = {}) {
  return {
    id: approvalId,
    tool_name: 'send_email',
    input: { to: ['ana@example.com'], subject: 'Factura 42', body: 'Adjunto la factura.', cc: [], bcc: [], thread_id: null },
    action_key: 'send_email:0f1e',
    required_role: 'operator',
    status: 'pending',
    requested_by: 1,
    created_at: '2026-10-15T09:00:00Z',
    expires_at: '2026-10-15T10:00:00Z',
    decided_by: null,
    decided_at: null,
    decision_reason: null,
    executed_by: null,
    executed_at: null,
    execution_result: null,
    execution_error: null,
    ...overrides,
  };
}
