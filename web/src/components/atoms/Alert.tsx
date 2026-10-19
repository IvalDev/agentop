import type { ReactNode } from 'react';

export interface AlertProps {
  /** `error` is announced at once (role="alert"); `info` politely (role="status"). */
  tone: 'error' | 'info';
  children: ReactNode;
}

export function Alert({ tone, children }: AlertProps) {
  return (
    <div role={tone === 'error' ? 'alert' : 'status'} className={`alert alert--${tone}`}>
      {children}
    </div>
  );
}
