import { queryClient } from '@/app/query-client';
import { readServerActiveTurns } from './server-active-turn';
import { readActiveTurnFor } from './active-turn';
import { resumeActiveTurn } from './resume-turn';

export const CHAT_RECONCILE_INTERVAL_MS = 30_000;
export const CHAT_RECONCILED_EVENT = 'vibecanvas:chat-reconciled';

export interface ReconcileChatArgs {
  wfId: string | null | undefined;
  chatId?: string | null;
  surface?: 'chat' | 'browser';
}

async function refreshActiveProjection(queryKey: readonly unknown[]): Promise<void> {
  await queryClient.invalidateQueries({ queryKey });
  await queryClient.refetchQueries({ queryKey, type: 'active' });
}

/**
 * Reconcile chat state after a disconnected or backgrounded frontend returns.
 *
 * The backend is the authority for chat history, active runs, and pending HITL.
 * This function deliberately does not infer state from localStorage or current
 * component state. It contacts the active-run endpoint, resumes any live turns,
 * and refreshes the server-backed chat projections so a tab that was offline
 * while another tab continued the conversation catches up automatically.
 */
export async function reconcileChatWithServer({
  wfId,
  chatId,
  surface = 'chat',
}: ReconcileChatArgs): Promise<void> {
  if (!wfId) return;

  const turns = await readServerActiveTurns(wfId);
  if (turns) {
    for (const turn of turns) {
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
    const localTurn = chatId ? readActiveTurnFor(wfId, chatId) : null;
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
