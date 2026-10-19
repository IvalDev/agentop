import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from 'react';
import { streamChat } from '../api/chat';
import { useSession } from '../session/SessionProvider';
import { buildHistory, conversationReducer, requestErrorKind, type ChatExchange } from './conversation';
import { announceAssistantResponseDone } from './responseNotifications';

export interface ChatValue {
  exchanges: ChatExchange[];
  /** A reply is streaming: sending waits, Detener is offered. */
  busy: boolean;
  /** Send a trimmed, non-empty message with the conversation so far; ignored while busy. */
  send(message: string): void;
  /** Abort the streaming reply, keeping the text that had arrived. */
  stop(): void;
  /** Abort any reply and forget the conversation. */
  reset(): void;
}

const ChatContext = createContext<ChatValue | null>(null);

interface ActiveRun {
  id: number;
  controller: AbortController;
}

/**
 * The conversation of this session, in memory only. Mounted by the dashboard shell, so it
 * survives visiting other sections and is dropped (aborting any reply) when the session ends.
 */
export function ChatProvider({ children }: { children: ReactNode }) {
  const { api } = useSession();
  const [exchanges, dispatch] = useReducer(conversationReducer, []);
  const exchangesRef = useRef(exchanges);
  const activeRef = useRef<ActiveRun | null>(null);
  const nextIdRef = useRef(1);

  useEffect(() => {
    exchangesRef.current = exchanges;
  }, [exchanges]);

  useEffect(() => () => activeRef.current?.controller.abort(), []);

  const send = useCallback(
    (message: string) => {
      if (activeRef.current !== null) {
        return;
      }
      const id = nextIdRef.current;
      nextIdRef.current += 1;
      const controller = new AbortController();
      const run: ActiveRun = { id, controller };
      activeRef.current = run;
      const history = buildHistory(exchangesRef.current);
      dispatch({ type: 'send', id, question: message });

      void (async () => {
        try {
          for await (const event of streamChat(api, message, history, controller.signal)) {
            if (controller.signal.aborted) {
              break;
            }
            dispatch({ type: 'event', id, event });
            if (event.type === 'done') {
              // A window that is not in front may want to notify; in a browser nobody listens.
              announceAssistantResponseDone(event.answer);
              break; // the turn is settled: free the composer at once
            }
            if (event.type === 'error') {
              break; // the turn is settled: free the composer at once
            }
          }
          dispatch(controller.signal.aborted ? { type: 'stop', id } : { type: 'end', id });
        } catch (error) {
          dispatch(
            controller.signal.aborted ? { type: 'stop', id } : { type: 'fail', id, error: requestErrorKind(error) },
          );
        } finally {
          if (activeRef.current === run) {
            activeRef.current = null;
          }
        }
      })();
    },
    [api],
  );

  const stop = useCallback(() => {
    const run = activeRef.current;
    if (run !== null) {
      activeRef.current = null;
      run.controller.abort();
      dispatch({ type: 'stop', id: run.id });
    }
  }, []);

  const reset = useCallback(() => {
    activeRef.current?.controller.abort();
    activeRef.current = null;
    dispatch({ type: 'reset' });
  }, []);

  const busy = exchanges.some((exchange) => exchange.status === 'streaming');
  const value = useMemo<ChatValue>(
    () => ({ exchanges, busy, send, stop, reset }),
    [exchanges, busy, send, stop, reset],
  );
  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat(): ChatValue {
  const value = useContext(ChatContext);
  if (value === null) {
    throw new Error('useChat must be used inside <ChatProvider>');
  }
  return value;
}
