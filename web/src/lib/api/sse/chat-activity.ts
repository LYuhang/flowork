import { fetchEventSource } from '@microsoft/fetch-event-source';
import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import { useChatStreamStore } from '@/stores/chat-stream';
import { readServerActiveTurns } from './server-active-turn';
import { reconcileChatWithServer, type ReconcileChatArgs } from './chat-reconcile';

/** Each window subscribes independently; reconnect refreshes durable state. */
export function watchChatActivity(args: ReconcileChatArgs): () => void {
  if (!args.wfId || !args.chatId) return () => {};
  const ctrl = new AbortController();
  let dirty = false;
  let refreshing = false;
  const refresh = async () => {
    dirty = true;
    if (refreshing) return;
    refreshing = true;
    try {
      // A completion arriving during the start snapshot must cause another
      // read. Sharing just the in-flight promise would lose that invalidation.
      while (dirty && !ctrl.signal.aborted) {
        dirty = false;
        await reconcileChatWithServer(args);
      }
    } finally {
      refreshing = false;
    }
  };
  const token = useAuthStore.getState().token;
  // Lifecycle notifications cannot report a crashed worker. Keep lease
  // reconciliation only for running turns; idle chats issue no timed reads.
  // The server expires orphan runs and emits a durable terminal event.
  let checkingLease = false;
  const leaseTimer = window.setInterval(() => {
    if (checkingLease || useChatStreamStore.getState().runtimes[args.chatId!]?.state !== 'streaming') return;
    checkingLease = true;
    void readServerActiveTurns(args.wfId!).finally(() => { checkingLease = false; });
  }, 60_000);
  ctrl.signal.addEventListener('abort', () => window.clearInterval(leaseTimer), { once: true });
  void fetchEventSource(
    `${getApiBase()}/api/v1/chat-scopes/${encodeURIComponent(args.wfId)}/chats/${encodeURIComponent(args.chatId)}/activity`,
    {
      signal: ctrl.signal,
      credentials: 'include',
      headers: { Accept: 'text/event-stream', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      async onopen(response) {
        if ([401, 403, 404].includes(response.status)) ctrl.abort();
        if (response.status === 401) useAuthStore.getState().handle401();
        if (!response.ok || !response.headers.get('content-type')?.startsWith('text/event-stream')) {
          throw new Error('Chat activity connection failed');
        }
      },
      onmessage(message) {
        if (message.event === 'changed') void refresh().catch(() => {});
      },
      onclose() { throw new Error('Chat activity disconnected'); },
      onerror() { return 2000; },
    },
  ).catch(() => {});
  return () => {
    window.clearInterval(leaseTimer);
    ctrl.abort();
  };
}
