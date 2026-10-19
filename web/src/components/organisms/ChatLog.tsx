import type { ChatExchange } from '../../chat/conversation';
import { t } from '../../i18n';
import { ChatTurn } from './ChatTurn';

export interface ChatLogProps {
  exchanges: ChatExchange[];
}

const TITLE_ID = 'chat-log-title';

/**
 * The conversation as a `log`. Its implicit live region is off: streamed tokens would be
 * read one by one, so the page announces each finished answer once instead.
 */
export function ChatLog({ exchanges }: ChatLogProps) {
  return (
    <div className="chat-log">
      <h2 id={TITLE_ID} className="visually-hidden">
        {t('chat.logLabel')}
      </h2>
      <div role="log" aria-labelledby={TITLE_ID} aria-live="off" className="chat-log__turns">
        {exchanges.length === 0 ? (
          <p className="chat-log__empty">{t('chat.empty')}</p>
        ) : (
          exchanges.map((exchange) => <ChatTurn key={exchange.id} exchange={exchange} />)
        )}
      </div>
    </div>
  );
}
