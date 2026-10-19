import { describe, expect, it } from 'vitest';
import {
  ASSISTANT_RESPONSE_DONE_EVENT,
  ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS,
  announceAssistantResponseDone,
  responsePreview,
} from './responseNotifications';

describe('responsePreview', () => {
  it('trims the answer and keeps a short one whole', () => {
    expect(responsePreview('  Tienes 23 días laborables.  ')).toBe('Tienes 23 días laborables.');
  });

  it('caps the preview, counting astral characters once', () => {
    const answer = '😀'.repeat(ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS + 10);

    const preview = responsePreview(answer);

    expect(Array.from(preview ?? '')).toHaveLength(ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS);
    expect(preview).toBe('😀'.repeat(ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS));
  });

  it('has no preview for an empty or blank answer', () => {
    expect(responsePreview('')).toBeUndefined();
    expect(responsePreview('  \n  ')).toBeUndefined();
  });
});

describe('announceAssistantResponseDone', () => {
  function capture(): { details: unknown[]; listener: (event: Event) => void } {
    const details: unknown[] = [];
    const listener = (event: Event) => {
      details.push((event as CustomEvent).detail);
    };
    window.addEventListener(ASSISTANT_RESPONSE_DONE_EVENT, listener);
    return { details, listener };
  }

  it('dispatches the completion event on window with the trimmed preview', () => {
    const { details, listener } = capture();
    try {
      announceAssistantResponseDone('  Listo.  ');

      expect(details).toStrictEqual([{ preview: 'Listo.' }]);
    } finally {
      window.removeEventListener(ASSISTANT_RESPONSE_DONE_EVENT, listener);
    }
  });

  it('dispatches a null-safe detail when the answer is empty', () => {
    const { details, listener } = capture();
    try {
      announceAssistantResponseDone('   ');

      expect(details).toStrictEqual([{ preview: undefined }]);
    } finally {
      window.removeEventListener(ASSISTANT_RESPONSE_DONE_EVENT, listener);
    }
  });

  it('is a harmless no-op when nobody listens', () => {
    expect(() => announceAssistantResponseDone('Listo.')).not.toThrow();
  });
});
