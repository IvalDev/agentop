import type { ChatCitation } from '../../api/chat';
import { t } from '../../i18n';

export interface CitationListProps {
  /** Unique per answer: the heading's id, which also names the list. */
  headingId: string;
  citations: ChatCitation[];
  citationId: (number: number) => string;
}

/**
 * The passages an answer cites: document name, with the excerpt behind a disclosure.
 * A named list rather than a region, so many answers do not repeat a "Fuentes" landmark.
 */
export function CitationList({ headingId, citations, citationId }: CitationListProps) {
  return (
    <div className="citations">
      <h3 id={headingId} className="citations__title">
        {t('chat.sources')}
      </h3>
      <ol className="citations__list" aria-labelledby={headingId}>
        {citations.map((citation) => (
          <li key={citation.number}>
            <details id={citationId(citation.number)} className="citation">
              <summary className="citation__summary">
                {t('chat.sourceSummary', { number: String(citation.number), document: citation.documentFilename })}
              </summary>
              <p className="citation__excerpt">{citation.content}</p>
            </details>
          </li>
        ))}
      </ol>
    </div>
  );
}
