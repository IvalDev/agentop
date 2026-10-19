import { describe, expect, it, vi } from 'vitest';
import { chunkBytes, doneFrame, installChatApi, sseFrame } from '../test/fakeChat';
import {
  browserTimeZone,
  parseChatEvent,
  readServerSentEvents,
  streamChat,
  type ServerSentEvent,
} from './chat';
import { createApiClient } from './client';

function bodyOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      controller.close();
    },
  });
}

/** Make `Intl.DateTimeFormat().resolvedOptions()` report `timeZone` (or throw). */
function stubTimeZone(timeZone: string | Error) {
  const real = Intl.DateTimeFormat.prototype.resolvedOptions;
  vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockImplementation(function (
    this: Intl.DateTimeFormat,
  ) {
    if (timeZone instanceof Error) {
      throw timeZone;
    }
    return { ...real.call(this), timeZone };
  });
}

async function collect<T>(events: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of events) {
    items.push(item);
  }
  return items;
}

describe('readServerSentEvents', () => {
  it('reassembles frames cut at every byte, inside lines and inside UTF-8 characters', async () => {
    const wire = sseFrame('token', { text: 'Año 2026: 23 días 🌴' }) + doneFrame('Listo');

    const events = await collect(readServerSentEvents(bodyOf(chunkBytes(wire, 1))));

    expect(events.map((event) => event.type)).toEqual(['token', 'done']);
    expect(JSON.parse(events[0]!.data)).toEqual({ text: 'Año 2026: 23 días 🌴' });
  });

  it('accepts CRLF, joins multi-line data, skips comments, and flushes an unterminated last frame', async () => {
    const wire = ': keep-alive\r\nevent: token\r\ndata: {"text":\r\ndata: "hola"}\r\n\r\nevent: done\ndata: {"answer":"x"}';

    const events = await collect(readServerSentEvents(bodyOf(chunkBytes(wire, 4))));

    expect(events).toEqual<ServerSentEvent[]>([
      { type: 'token', data: '{"text":\n"hola"}' },
      { type: 'done', data: '{"answer":"x"}' },
    ]);
  });
});

describe('parseChatEvent', () => {
  it('types the payloads and keeps only what the UI shows', () => {
    const citation = {
      number: 2,
      document_id: 'd',
      document_filename: 'manual.pdf',
      chunk_id: 'c',
      chunk_index: 0,
      start_offset: 0,
      end_offset: 5,
      content: 'texto',
    };
    const approval = { approval_id: 'a-1', tool_name: 'register_invoice', expires_at: '2026-10-15T10:00:00Z' };

    expect(parseChatEvent({ type: 'tool_call', data: '{"call_id":"c1","tool_name":"search_documents"}' })).toEqual({
      type: 'tool_call',
      toolName: 'search_documents',
    });
    expect(parseChatEvent({ type: 'approval_required', data: JSON.stringify(approval) })).toEqual({
      type: 'approval_required',
      approval: { approvalId: 'a-1', toolName: 'register_invoice', expiresAt: '2026-10-15T10:00:00Z' },
    });
    expect(
      parseChatEvent({
        type: 'done',
        data: JSON.stringify({
          answer: 'Ver [2]',
          stop_reason: 'step_limit',
          citations: [citation, { number: 'x' }],
          pending_approvals: [approval],
        }),
      }),
    ).toEqual({
      type: 'done',
      answer: 'Ver [2]',
      stopReason: 'step_limit',
      citations: [{ number: 2, documentFilename: 'manual.pdf', content: 'texto' }],
      approvals: [{ approvalId: 'a-1', toolName: 'register_invoice', expiresAt: '2026-10-15T10:00:00Z' }],
    });
  });

  it('drops unknown types and malformed data', () => {
    expect(parseChatEvent({ type: 'surprise', data: '{}' })).toBeNull();
    expect(parseChatEvent({ type: 'token', data: 'not json' })).toBeNull();
    expect(parseChatEvent({ type: 'token', data: '["array"]' })).toBeNull();
    expect(parseChatEvent({ type: 'approval_required', data: '{"tool_name":"x"}' })).toBeNull();
  });
});

describe('streamChat', () => {
  it('posts the message and history with the session token and yields typed events', async () => {
    const { fetchMock, requests } = installChatApi('viewer', [
      sseFrame('run_started', { run_id: 'r' }) + sseFrame('token', { text: 'Hola' }) + doneFrame('Hola'),
    ]);
    const api = createApiClient({ getToken: () => 'viewer-token', onUnauthorized: vi.fn() });
    const history = [{ role: 'user' as const, content: 'Antes' }];

    const events = await collect(streamChat(api, '¿Qué tal?', history, new AbortController().signal));

    expect(events.map((event) => event.type)).toEqual(['run_started', 'token', 'done']);
    expect(requests[0]).toMatchObject({ message: '¿Qué tal?', history });
    const init = fetchMock.mock.calls[0]![1]!;
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer viewer-token');
    expect(new Headers(init.headers).get('Accept')).toBe('text/event-stream');
  });

  it('sends the browser time zone so the agent knows the local date', async () => {
    stubTimeZone('Europe/Madrid');
    const { requests } = installChatApi('viewer', [doneFrame('Hola')]);
    const api = createApiClient({ getToken: () => 'viewer-token', onUnauthorized: vi.fn() });

    await collect(streamChat(api, 'Hoy', [], new AbortController().signal));

    expect(requests[0]).toMatchObject({ message: 'Hoy', history: [], timezone: 'Europe/Madrid' });
  });

  it('omits the time zone when the browser cannot resolve one', async () => {
    stubTimeZone(new RangeError('no time zone data'));
    const { requests } = installChatApi('viewer', [doneFrame('Hola')]);
    const api = createApiClient({ getToken: () => 'viewer-token', onUnauthorized: vi.fn() });

    await collect(streamChat(api, 'Hoy', [], new AbortController().signal));

    expect(requests[0]).toEqual({ message: 'Hoy', history: [], signal: expect.anything() });
    expect(requests[0]).not.toHaveProperty('timezone');
  });
});

describe('browserTimeZone', () => {
  it('returns the IANA name the browser resolves', () => {
    stubTimeZone('America/Argentina/Buenos_Aires');

    expect(browserTimeZone()).toBe('America/Argentina/Buenos_Aires');
  });

  it('returns undefined when the zone is empty or the lookup throws', () => {
    stubTimeZone('');
    expect(browserTimeZone()).toBeUndefined();

    vi.restoreAllMocks();
    stubTimeZone(new RangeError('no time zone data'));
    expect(browserTimeZone()).toBeUndefined();
  });
});
