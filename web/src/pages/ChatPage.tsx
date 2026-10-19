import { useRef, useState } from 'react';
import { ChatComposer } from '../components/organisms/ChatComposer';
import { ChatLog } from '../components/organisms/ChatLog';
import { useChat } from '../chat/ChatProvider';
import { announcementFor } from '../chat/messages';
import { useFollowBottom } from '../hooks/useFollowBottom';
import { usePageTitle } from '../hooks/usePageTitle';
import { t } from '../i18n';

/** Chat section: streams the agent's answers with their sources, for every role. */
export function ChatPage() {
  const title = t('sections.chat.title');
  usePageTitle(title);
  const { exchanges, busy, send, stop, reset } = useChat();
  const [justReset, setJustReset] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const followBottom = useFollowBottom(endRef, exchanges);

  const last = exchanges.at(-1);
  const announcement = last ? announcementFor(last) : justReset ? t('chat.resetDone') : '';
  // A new node per turn and state, so the same text twice is still announced.
  const announcementKey = last ? `${last.id}-${last.status}-${last.activity ?? ''}` : String(justReset);

  return (
    <div className="page chat">
      <h1 className="page__title">{title}</h1>
      <p className="page__lead">{t('sections.chat.description')}</p>
      <ChatLog exchanges={exchanges} />
      <ChatComposer
        busy={busy}
        onSend={(message) => {
          setJustReset(false);
          followBottom();
          send(message);
        }}
        onStop={stop}
        onReset={() => {
          reset();
          setJustReset(true);
        }}
      />
      <div ref={endRef} />
      <div role="status" className="visually-hidden">
        <p key={announcementKey}>{announcement}</p>
      </div>
    </div>
  );
}
