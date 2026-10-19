import { Button, type ButtonProps } from '../atoms/Button';

export type RowActionButtonProps = Omit<ButtonProps, 'children'> & {
  /** The visible verb, e.g. "Reintentar". */
  label: string;
  /** What it acts on, for screen readers only, e.g. "«informe.pdf»". */
  target: string;
  /** The action is running: the button stays focusable but does nothing. */
  busy?: boolean;
};

/**
 * A button in a table row: it shows the verb and names its row for screen readers, so a list
 * of "Reintentar" buttons is still distinguishable. The visible text starts the name (WCAG 2.5.3).
 */
export function RowActionButton({ label, target, busy = false, onClick, variant = 'secondary', ...rest }: RowActionButtonProps) {
  return (
    <Button variant={variant} aria-disabled={busy || undefined} onClick={busy ? undefined : onClick} {...rest}>
      {label}{' '}
      <span className="visually-hidden">{target}</span>
    </Button>
  );
}
