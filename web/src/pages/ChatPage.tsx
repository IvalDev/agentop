import { SectionPlaceholder } from '../components/templates/SectionPlaceholder';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';

/** Chat section; L3 replaces the placeholder with streaming chat. */
export function ChatPage() {
  const title = t('sections.chat.title');
  usePageTitle(title);
  return <SectionPlaceholder title={title} description={t('sections.chat.description')} />;
}
