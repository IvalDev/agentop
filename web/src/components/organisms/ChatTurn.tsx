import type { ChatExchange } from '../../chat/conversation';
import { activityMessage, statusMessage } from '../../chat/messages';
import { t } from '../../i18n';
import { ApprovalNotice } from '../molecules/ApprovalNotice';
import { CitationList } from '../molecules/CitationList';
import { CitedText } from '../molecules/CitedText';

export interface ChatTurnProps {
  exchange: ChatExchange;
}

/**
 * One question and its reply: activity, answer text, notices, the confirmation of each action
 * the agent asked for, and cited sources.
 */
export function ChatTurn({ exchange }: ChatTurnProps) {
  const prefix = `chat-${exchange.id}`;
  const citationId = (number: number) => `${prefix}-source-${number}`;
  const status = statusMessage(exchange);
  // At the step limit the answer is the API's fixed English fallback: the notice replaces it.
  const showAnswer = exchange.answer !== '' && exchange.status !== 'stepLimit';

  function openCitation(number: number) {
    const details = document.getElementById(citationId(number));
    if (details instanceof HTMLDetailsElement) {
      details.open = true;
      details.querySelector('summary')?.focus();
    }
  }

  return (
    <div className="chat-turn">
      <div className="chat-message chat-message--user">
        <p className="chat-message__author">{t('chat.you')}</p>
        <p className="chat-message__text">{exchange.question}</p>
      </div>
      <div className="chat-message chat-message--assistant">
        <p className="chat-message__author">{t('chat.assistant')}</p>
        {exchange.activity && <p className="chat-message__activity">{activityMessage(exchange.activity)}</p>}
        {showAnswer && (
          <CitedText
            text={exchange.answer}
            citations={exchange.citations}
            citationId={citationId}
            onOpenCitation={openCitation}
          />
        )}
        {status && <p className={`chat-notice chat-notice--${status.tone}`}>{status.text}</p>}
        {exchange.approvals.map((approval) => (
          <ApprovalNotice key={approval.approvalId} approval={approval} />
        ))}
        {exchange.citations.length > 0 && (
          <CitationList headingId={`${prefix}-sources`} citations={exchange.citations} citationId={citationId} />
        )}
      </div>
    </div>
  );
}
