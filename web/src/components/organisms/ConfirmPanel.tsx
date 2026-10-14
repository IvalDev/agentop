import { useEffect, useRef, type FormEvent, type ReactNode } from 'react';
import { Button } from '../atoms/Button';

export interface ConfirmPanelProps {
  /** Id of the heading, which names the panel. */
  titleId: string;
  title: string;
  confirmLabel: string;
  cancelLabel: string;
  /** The action is running: confirming again does nothing. */
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  /** What the action does, and any field it needs. */
  children?: ReactNode;
}

/**
 * An inline confirmation step: a named region whose heading takes focus when it opens, so
 * keyboard and screen reader users land on it. Remount it (`key`) for each new action.
 */
export function ConfirmPanel(props: ConfirmPanelProps) {
  const { titleId, title, confirmLabel, cancelLabel, busy, onConfirm, onCancel, children } = props;
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!busy) {
      onConfirm();
    }
  }

  return (
    <section className="confirm-panel" aria-labelledby={titleId}>
      <h3 id={titleId} ref={headingRef} tabIndex={-1} className="confirm-panel__title">
        {title}
      </h3>
      <form className="confirm-panel__form" noValidate onSubmit={handleSubmit}>
        {children}
        <div className="actions">
          <Button type="submit" aria-disabled={busy || undefined}>
            {confirmLabel}
          </Button>
          <Button variant="secondary" onClick={onCancel}>
            {cancelLabel}
          </Button>
        </div>
      </form>
    </section>
  );
}
