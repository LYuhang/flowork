import { useCallback, useMemo, useRef, useState } from 'react';
import { CHAT_INITIAL_HISTORY_LIMIT, fetchChatHistoryPage, useChatHistory } from '@/lib/api/queries/chats';
import { mergeHistoryWindow, type ChatHistoryWindow } from '@/pages/chat/history-window';
import { useChatStreamStore } from '@/stores/chat-stream';

/** Shared persisted transcript window; stream projection and scroll anchoring
 * remain owned by the existing Chat store and ChatMessageList. */
export function useConversationHistory(scopeId: string, chatId: string | null, persisted: boolean) {
  const turnId = useChatStreamStore(state => {
    const runtime = chatId ? state.runtimes[chatId] : undefined;
    return runtime?.projectionActive ? runtime.turnId : null;
  });
  const query = useChatHistory(scopeId, persisted ? chatId : null, persisted, turnId);
  const key = `${scopeId}:${chatId ?? ''}`;
  const [windows, setWindows] = useState<Record<string, ChatHistoryWindow>>({});
  const window = useMemo(() => query.data ? mergeHistoryWindow(windows[key], query.data) : windows[key],
    [key, query.data, windows]);
  const [checkpoint, setCheckpoint] = useState<{ key: string; page: typeof query.data } | null>(null);
  // Capture a changed query page during this render so the next Turn can
  // immediately reuse it, without an effect-driven second commit.
  if (query.data && chatId && (checkpoint?.key !== key || checkpoint.page !== query.data)) {
    setCheckpoint({ key, page: query.data });
    setWindows(current => {
      const next = { ...current };
      delete next[key];
      next[key] = mergeHistoryWindow(current[key], query.data);
      return Object.fromEntries(Object.entries(next).slice(-20));
    });
  }
  const pending = useRef(new Set<string>());
  const [loadingKey, setLoadingKey] = useState<string | null>(null);
  const loadOlder = useCallback(async () => {
    if (!chatId || !window || window.offset <= 0 || pending.current.has(key)) return;
    pending.current.add(key);
    setLoadingKey(key);
    try {
      const limit = Math.min(CHAT_INITIAL_HISTORY_LIMIT, window.offset);
      const page = await fetchChatHistoryPage(scopeId, chatId, {
        limit, offset: Math.max(0, window.offset - limit), ...(turnId ? { beforeTurnId: turnId } : {}),
      });
      setWindows(current => {
        const next = { ...current };
        delete next[key];
        next[key] = mergeHistoryWindow(current[key], page);
        return Object.fromEntries(Object.entries(next).slice(-20));
      });
    } finally {
      pending.current.delete(key);
      setLoadingKey(current => current === key ? null : current);
    }
  }, [chatId, key, scopeId, turnId, window]);
  return { query, items: window?.items, hasOlder: !!window && window.offset > 0,
    loadingOlder: loadingKey === key, loadOlder,
    ready: !persisted || query.data !== undefined };
}
