import type { ComponentPropsWithRef } from 'react';

export type TextFieldProps = Omit<ComponentPropsWithRef<'input'>, 'id' | 'aria-invalid' | 'aria-describedby'> & {
  id: string;
  label: string;
  hint?: string;
  /** Translated message; marks the input invalid and describes it. */
  error?: string | null;
};

/** Labelled input whose hint and error are announced as its description. */
export function TextField({ id, label, hint, error, className, ...inputProps }: TextFieldProps) {
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
      <input
        id={id}
        className="field__input"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...inputProps}
      />
      {error && (
        <p id={errorId} className="field__error">
          {error}
        </p>
      )}
    </div>
  );
}
