import type { ReactNode, Ref } from 'react';
import { t } from '../../i18n';
import { SkipLink } from '../atoms/SkipLink';

export const MAIN_CONTENT_ID = 'main-content';

export interface DashboardLayoutProps {
  header: ReactNode;
  children: ReactNode;
  /** Receives focus after client-side navigation. */
  mainRef?: Ref<HTMLElement>;
}

export function DashboardLayout({ header, children, mainRef }: DashboardLayoutProps) {
  return (
    <div className="dashboard">
      <SkipLink targetId={MAIN_CONTENT_ID}>{t('app.skipToContent')}</SkipLink>
      {header}
      <main id={MAIN_CONTENT_ID} ref={mainRef} tabIndex={-1} className="dashboard__main">
        {children}
      </main>
    </div>
  );
}
