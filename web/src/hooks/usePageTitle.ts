import { useEffect } from 'react';
import { t } from '../i18n';

/** Set `document.title` to "<page> · <app name>" while the page is shown. */
export function usePageTitle(page: string): void {
  useEffect(() => {
    document.title = t('app.pageTitle', { page, app: t('app.name') });
  }, [page]);
}
