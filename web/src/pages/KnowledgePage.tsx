import { SectionPlaceholder } from '../components/templates/SectionPlaceholder';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';

/** Knowledge base section; L4 replaces the placeholder with documents and uploads. */
export function KnowledgePage() {
  const title = t('sections.knowledge.title');
  usePageTitle(title);
  return <SectionPlaceholder title={title} description={t('sections.knowledge.description')} />;
}
