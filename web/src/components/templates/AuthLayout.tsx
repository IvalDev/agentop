import type { ReactNode } from 'react';
import { t } from '../../i18n';

export interface AuthLayoutProps {
  title: string;
  intro: string;
  children: ReactNode;
}

/** Centred panel for screens before the session exists (login). */
export function AuthLayout({ title, intro, children }: AuthLayoutProps) {
  return (
    <main className="auth">
      <div className="auth__panel">
        <p className="auth__brand">{t('app.name')}</p>
        <h1 className="auth__title">{title}</h1>
        <p className="auth__intro">{intro}</p>
        {children}
      </div>
    </main>
  );
}
