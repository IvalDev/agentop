import type { ComponentPropsWithRef } from 'react';

export type TextAreaFieldProps = Omit<
  ComponentPropsWithRef<'textarea'>,
  'id' | 'aria-invalid' | 'aria-describedby'
> & {
  id: string;
  label: string;
  hint?: string;
  /** Translated message; marks the field invalid and describes it. */
  error?: string | null;
};

/** Labelled multi-line field whose hint and error are announced as its description. */
export function TextAreaField({ id, label, hint, error, className, ...textareaProps }: TextAreaFieldProps) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;
  return (
    <div className={['field', className].filter(Boolean).join(' ')}>
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {hint && (
        <p id={hintId} className="field__hint">
          {hint}
        </p>
      )}
      <textarea
        id={id}
        className="field__input field__input--multiline"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...textareaProps}
      />
      {error && (
        <p id={errorId} className="field__error">
          {error}
        </p>
      )}
    </div>
  );
}
