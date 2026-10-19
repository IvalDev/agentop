import type { MouseEvent, ReactNode } from 'react';
import type { ChatCitation } from '../../api/chat';
import { t } from '../../i18n';

export interface CitedTextProps {
  /** Untrusted model text: rendered as text, never parsed as Markdown or HTML. */
  text: string;
  citations: ChatCitation[];
  /** Element id of citation `number` in the list below the answer. */
  citationId: (number: number) => string;
  onOpenCitation: (number: number) => void;
}

const MARKER = /\[(\d+)\]/g;

/** The answer as plain text; each `[n]` with a known citation becomes a link to it. */
export function CitedText({ text, citations, citationId, onOpenCitation }: CitedTextProps) {
  const parts: ReactNode[] = [];
  let shown = 0;
  for (const match of text.matchAll(MARKER)) {
    const number = Number(match[1]);
    const citation = citations.find((item) => item.number === number);
    if (citation === undefined) {
      continue;
    }
    parts.push(text.slice(shown, match.index));
    parts.push(
      <a
        key={match.index}
        href={`#${citationId(number)}`}
        className="cited-text__marker"
        aria-label={t('chat.sourceLink', { number: String(number), document: citation.documentFilename })}
        onClick={(event: MouseEvent<HTMLAnchorElement>) => {
          event.preventDefault();
          onOpenCitation(number);
        }}
      >
        {match[0]}
      </a>,
    );
    shown = match.index + match[0].length;
  }
  parts.push(text.slice(shown));
  return <p className="cited-text">{parts}</p>;
}
