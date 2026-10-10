import { useChatStreamStore } from '@/stores/chat-stream';
import { readServerActiveTurns } from './server-active-turn';
import { reconcileChatWithServer, type ReconcileChatArgs } from './chat-reconcile';
import { watchResourceActivity } from './resource-activity';

/** Each window subscribes independently; only failed reads are retried. */
export function watchChatActivity(args: ReconcileChatArgs): () => void {
  if (!args.wfId || !args.chatId) return () => {};
  // Notifications cannot report a crashed worker. Only active turns need a
  // periodic lease check; this does not submit or replay business operations.
  let checkingLease = false;
  const leaseTimer = window.setInterval(() => {
    if (checkingLease || useChatStreamStore.getState().runtimes[args.chatId!]?.state !== 'streaming') return;
    checkingLease = true;
    void readServerActiveTurns(args.wfId!).finally(() => { checkingLease = false; });
  }, 60_000);
  const stopActivity = watchResourceActivity(
    `/api/v1/chat-scopes/${encodeURIComponent(args.wfId)}/chats/${encodeURIComponent(args.chatId)}/activity`,
    () => reconcileChatWithServer(args, { throwOnError: true }),
    100,
    () => window.clearInterval(leaseTimer),
  );
  return () => {
    window.clearInterval(leaseTimer);
    stopActivity();
  };
}
