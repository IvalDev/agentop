/**
 * The "an assistant response just completed" announcement, as a plain DOM event.
 *
 * The SPA never talks to the desktop shell: it dispatches this event on `window` and
 * whoever listens may react. In a browser nobody listens, so it is a harmless no-op;
 * the widget window injects a bridge script (`desktop/src-tauri/src/notifications.rs`)
 * that forwards it to the native side, which decides whether to show a notification.
 */

/** Event name dispatched on `window` when a reply reaches `done`. */
export const ASSISTANT_RESPONSE_DONE_EVENT = 'aa:assistant-response-done';

/** Longest preview carried by the event, in code points; the shell clamps again. */
export const ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS = 140;

/**
 * The preview of a finished answer: trimmed, and cut to
 * `ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS` code points. `undefined` when there is
 * nothing worth showing (blank answer).
 */
export function responsePreview(answer: string): string | undefined {
  const trimmed = answer.trim();
  if (trimmed === '') {
    return undefined;
  }
  const characters = Array.from(trimmed);
  return characters.length > ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS
    ? characters.slice(0, ASSISTANT_RESPONSE_PREVIEW_MAX_CHARS).join('')
    : trimmed;
}

/**
 * Announce that the assistant finished a reply, with a short preview of it.
 * Called when the stream reaches `done` and the answer is authoritative.
 */
export function announceAssistantResponseDone(answer: string): void {
  window.dispatchEvent(
    new CustomEvent(ASSISTANT_RESPONSE_DONE_EVENT, { detail: { preview: responsePreview(answer) } }),
  );
}
