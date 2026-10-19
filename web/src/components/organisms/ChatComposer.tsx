import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { CHAT_MESSAGE_MAX_CHARS } from '../../api/chat';
import { t } from '../../i18n';
import { Button } from '../atoms/Button';
import { TextAreaField } from '../molecules/TextAreaField';

export interface ChatComposerProps {
  /** A reply is streaming: sending waits and Detener is offered. */
  busy: boolean;
  /** Receives the trimmed, non-empty message. */
  onSend: (message: string) => void;
  onStop: () => void;
  onReset: () => void;
}

/** The message field: Enter sends, Shift+Enter adds a line; focus stays in the field. */
export function ChatComposer({ busy, onSend, onStop, onReset }: ChatComposerProps) {
  const [message, setMessage] = useState('');
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  function focusInput() {
    inputRef.current?.focus();
  }

  function submit() {
    if (busy) {
      return;
    }
    const trimmed = message.trim();
    if (!trimmed) {
      setError(t('chat.messageRequired'));
      focusInput();
      return;
    }
    onSend(trimmed);
    setMessage('');
    setError(null);
    focusInput();
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    submit();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Not while an input method is composing a character (Enter confirms it there).
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <form className="chat-composer" noValidate onSubmit={handleSubmit}>
      <TextAreaField
        ref={inputRef}
        id="chat-message"
        label={t('chat.messageLabel')}
        hint={t('chat.messageHint')}
        error={error}
        value={message}
        onChange={(event) => {
          setMessage(event.target.value);
          setError(null);
        }}
        onKeyDown={handleKeyDown}
        rows={3}
        maxLength={CHAT_MESSAGE_MAX_CHARS}
        required
      />
      <div className="actions">
        <Button type="submit" aria-disabled={busy || undefined}>
          {t('chat.send')}
        </Button>
        {busy && (
          <Button
            variant="secondary"
            onClick={() => {
              onStop();
              focusInput();
            }}
          >
            {t('chat.stop')}
          </Button>
        )}
        <Button
          variant="secondary"
          onClick={() => {
            onReset();
            setError(null);
            focusInput();
          }}
        >
          {t('chat.reset')}
        </Button>
      </div>
    </form>
  );
}
