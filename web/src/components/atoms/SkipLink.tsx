import type { ReactNode } from 'react';

export interface SkipLinkProps {
  targetId: string;
  children: ReactNode;
}

/** First focusable element: hidden until focused, jumps past the header to `#targetId`. */
export function SkipLink({ targetId, children }: SkipLinkProps) {
  return (
    <a className="skip-link" href={`#${targetId}`}>
      {children}
    </a>
  );
}
