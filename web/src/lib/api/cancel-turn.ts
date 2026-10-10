// Cancel an in-flight agent turn (the STOP button).
//
// Stop is backend-owned: the frontend asks the backend to cancel the turn, then
// keeps reading the SSE stream until the backend emits closure frames and the
// terminal `error(code="cancelled")` frame. That terminal signal resets the Stop
// button state via route-signal; the frontend must not locally fabricate closure.
import { useAuthStore } from '@/stores/auth';
import { useChatStreamStore } from '@/stores/chat-stream';
import { toast } from 'sonner';
import i18n from '@/lib/i18n';
import { getApiBase } from '@/lib/base-path';

/**
 * Cancel the backend-confirmed active Run for one Chat.
 *
 * The backend resolves the Chat→Run binding, including after reload. A local
 * optimistic send must first receive its durable acknowledgement; its store
 * state is used only for that wait, never to select the backend cancellation
 * target. Failed sends and replaced local requests discard the waiting intent.
 */
export async function cancelActiveTurn(
  chatId: string,
): Promise<{ chatId: string; turnId: string } | null> {
  if (!chatId) return null;
  const pending = useChatStreamStore.getState().runtimes[chatId];
  // A local Send is optimistic. Wait for its first durable acknowledgement
  // before issuing Stop; do not race a cancellation against an uncommitted Run.
  if (pending?.state === 'streaming' && !pending.turnId && pending.abortController) {
    const accepted = await new Promise<boolean>(resolve => {
      const unsubscribe = useChatStreamStore.subscribe(state => {
        const runtime = state.runtimes[chatId];
        if (runtime?.abortController !== pending.abortController || runtime?.state !== 'streaming' || runtime.turnId) {
          unsubscribe();
          resolve(runtime?.abortController === pending.abortController && !!runtime.turnId);
        }
      });
    });
    if (!accepted) return null;
  }
  const token = useAuthStore.getState().token;
  const base = getApiBase();
  try {
    const response = await fetch(
      `${base}/api/v1/chats/${encodeURIComponent(chatId)}/active-turn/cancel`,
      {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      },
    );
    if (response.status === 401) {
      useAuthStore.getState().handle401();
      return null;
    }
    if (!response.ok) throw new Error(`Cancel failed (${response.status})`);
    const payload = await response.json() as { chat_id?: unknown; run_id?: unknown };
    if (payload.chat_id !== chatId || typeof payload.run_id !== 'string') return null;
    return { chatId, turnId: payload.run_id };
  } catch {
    toast.error(i18n.t('chat.stopFailed'));
    return null;
  }
}
