import { Link } from 'react-router';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';
import { paths } from '../routes/paths';

export function NotFoundPage() {
  usePageTitle(t('notFound.title'));
  return (
    <div className="page">
      <h1 className="page__title">{t('notFound.title')}</h1>
      <p className="page__lead">{t('notFound.description')}</p>
      <Link to={paths.chat}>{t('notFound.backToChat')}</Link>
    </div>
  );
}
