import { queryClient } from '@/app/query-client';
import { readServerActiveTurns } from './server-active-turn';
import { readActiveTurnFor } from './active-turn';
import { resumeActiveTurn } from './resume-turn';

export const CHAT_RECONCILED_EVENT = 'vibecanvas:chat-reconciled';

export interface ReconcileChatArgs {
  wfId: string | null | undefined;
  chatId?: string | null;
  surface?: 'chat' | 'browser';
}

async function refreshActiveProjection(queryKey: readonly unknown[]): Promise<void> {
  // Invalidation already refetches active observers. A second explicit fetch
  // doubles every history/list request on focus and periodic reconciliation.
  // Join an existing read instead of cancelling and restarting it.
  await queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false });
}

const pendingReconciliations = new Map<string, Promise<void>>();

/**
 * Reconcile chat state after a disconnected or backgrounded frontend returns.
 *
 * The backend is the authority for chat history, active runs, and pending HITL.
 * This function deliberately does not infer state from localStorage or current
 * component state. It contacts the active-run endpoint, resumes any live turns,
 * and refreshes the server-backed chat projections so a tab that was offline
 * while another tab continued the conversation catches up automatically.
 */
export function reconcileChatWithServer(args: ReconcileChatArgs): Promise<void> {
  if (!args.wfId) return Promise.resolve();
  const key = JSON.stringify([args.wfId, args.chatId ?? null, args.surface ?? 'chat']);
  const pending = pendingReconciliations.get(key);
  if (pending) return pending;
  // Focus, visibility, reconnect and the timer can all arrive together. Share
  // one reconciliation until it settles; later events still fetch fresh state.
  const next = reconcileChat(args).finally(() => {
    if (pendingReconciliations.get(key) === next) pendingReconciliations.delete(key);
  });
  pendingReconciliations.set(key, next);
  return next;
}

async function reconcileChat({
  wfId,
  chatId,
  surface = 'chat',
}: ReconcileChatArgs): Promise<void> {
  if (!wfId) return;

  // Discovery removes terminal markers from storage. Keep the replay cursor
  // before that read so a missed terminal frame can still be recovered.
  const localTurn = chatId ? readActiveTurnFor(wfId, chatId) : null;
  const turns = await readServerActiveTurns(wfId);
  if (turns) {
    for (const turn of turns) {
      if (chatId && turn.chatId !== chatId) continue;
      void resumeActiveTurn(turn);
    }
    // The POST stream can lose only its final terminal frame while the backend
    // has already committed the Run as complete. In that state the Run is no
    // longer returned by `active-runs`, but the page still owns a durable local
    // turn marker and may remain on "Agent is thinking" forever. Replay that
    // exact Turn once more: the read-only cursor stream supplies its persisted
    // done/error frame and the normal signal router closes the UI lifecycle.
    // The page-local stream coordinator makes this a no-op while the original
    // POST transport still owns the Chat, so periodic reconciliation cannot
    // create a competing projection.
    if (
      localTurn
      && !turns.some((turn) => (
        turn.chatId === localTurn.chatId && turn.turnId === localTurn.turnId
      ))
    ) {
      void resumeActiveTurn(localTurn);
    }
  }

  await Promise.allSettled([
    refreshActiveProjection(['chats', wfId, surface]),
    refreshActiveProjection(['chat-projects']),
    refreshActiveProjection(['project-sandboxes']),
    refreshActiveProjection(['project-mcp']),
    chatId
      ? refreshActiveProjection(['chat-history', wfId, chatId])
      : Promise.resolve(),
    chatId
      ? refreshActiveProjection(['chat-state', wfId, chatId])
      : Promise.resolve(),
    chatId
      ? refreshActiveProjection(['chat-workspace', chatId])
      : Promise.resolve(),
    chatId
      ? refreshActiveProjection(['browser-binding', chatId])
      : Promise.resolve(),
  ]);

  // History rows contain the creation-time ToolMessage projection. Existing
  // interactive cards may keep the same React key after refetch, so explicitly
  // ask them to rehydrate their authoritative artifact/HITL snapshot as well.
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(CHAT_RECONCILED_EVENT, {
      detail: { chatId: chatId ?? null },
    }));
  }
}
