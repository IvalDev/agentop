import { t } from '../../i18n';

export interface SectionPlaceholderProps {
  title: string;
  description: string;
}

/** Stand-in for a section that a later task fills (Chat L3, Knowledge base L4, Team L5). */
export function SectionPlaceholder({ title, description }: SectionPlaceholderProps) {
  return (
    <div className="page">
      <h1 className="page__title">{title}</h1>
      <p className="page__lead">{description}</p>
      <p className="page__note">{t('sections.comingSoon')}</p>
    </div>
  );
}
