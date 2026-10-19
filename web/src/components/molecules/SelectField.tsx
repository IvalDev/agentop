import type { ComponentPropsWithRef } from 'react';

export interface SelectOption {
  value: string;
  label: string;
}

export type SelectFieldProps = Omit<
  ComponentPropsWithRef<'select'>,
  'id' | 'children' | 'aria-invalid' | 'aria-describedby'
> & {
  id: string;
  label: string;
  options: SelectOption[];
  hint?: string;
  /** Translated message; marks the select invalid and describes it. */
  error?: string | null;
};

/** Labelled native select whose hint and error are announced as its description. */
export function SelectField({ id, label, options, hint, error, className, ...selectProps }: SelectFieldProps) {
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
      <select
        id={id}
        className="field__input"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...selectProps}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {error && (
        <p id={errorId} className="field__error">
          {error}
        </p>
      )}
    </div>
  );
}
