import { SectionPlaceholder } from '../components/templates/SectionPlaceholder';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';

/** Settings → Team (owner and admin only); L5 replaces the placeholder with user management. */
export function TeamPage() {
  const title = t('sections.team.title');
  usePageTitle(title);
  return <SectionPlaceholder title={title} description={t('sections.team.description')} />;
}
