/**
 * Agent-turn orchestrator — wraps {@link streamAgentTurn} with the
 * abort-controller / lastInput / state lifecycle that both the
 * {@link ChatComposer} Send/Retry buttons and the
 * {@link SSEStatusBanner} Retry button need.
 *
 * Extracted from `ChatComposer.doSend` so the banner doesn't need to
 * reach into the composer for retry — both call this and the
 * lifecycle stays in one place.
 *
 * Lifecycle:
 *   0. Save-before-send: if the canvas draft has unsaved edits, commit
 *      them first so the backend loads the user's current canvas state
 *      rather than the last saved version. Mirrors the save-before-run
 *      guard used by workflow execution. Fail-soft: a save error is
 *      toasted but the turn still starts (agent sees last saved version).
 *   1. Wire a fresh `AbortController` into the chat-stream store for real
 *      transport teardown paths. The Stop button does not abort it; Stop asks
 *      the backend to cancel the turn and leaves the stream open for closure
 *      frames.
 *   2. Stash `{ content, attachments }` as `lastInput` so a later
 *      Retry can replay the same payload.
 *   3. Reset state to `idle` — the route-signal `started` handler
 *      will flip it to `streaming` once the first chunk arrives.
 *      Resetting before send hides the Retry banner immediately
 *      instead of waiting for the first chunk.
 *   4. `await streamAgentTurn(...)`. AbortError is a transport interruption;
 *      backend user-cancel arrives as an SSE terminal frame and is handled in
 *      route-signal. Other errors → `failed` + toast.
 *   5. Clear the abort controller on completion so the store doesn't
 *      hold a stale reference.
 *
 * Does NOT mutate `pendingAttachments`. The composer atomically moves its
 * draft snapshot into this call before the request; Retry calls this helper
 * directly from `lastInput` and therefore has no composer chips to drain.
 */
import { toast } from 'sonner';
import type { components } from '@/lib/api/schema';
import { errorMessage } from '@/lib/api/mutations/error-message';
import { useChatStreamStore } from '@/stores/chat-stream';
import { streamAgentTurn, type HitlContinueControl } from './agent-stream';
import { getApprovalMode, type AgentSettings, type ApprovalMode } from '@/stores/agent-settings';

type Attachment = components['schemas']['Attachment'];

export interface RunAgentTurnArgs {
  wfId: string;
  chatId: string;
  projectId?: string | null;
  content: string;
  control?: HitlContinueControl;
  attachments?: Attachment[];
  /** Forwarded to the SSE body for browser-routed or ordinary Chat turns. */
  mode?: 'chat' | 'browser';
  /** Where the chat lives — "sidepanel" in the extension embed, "main" (default)
   *  in the main app. Threaded to the SSE body so the backend gates the
   *  side-panel-only `/browser` command. */
  surface?: 'main' | 'sidepanel';
  /** Product entry surface used by backend prompt/tool assembly. */
  agentSurface?: 'chat' | 'browser';
  approvalMode?: ApprovalMode;
  agentSettings?: AgentSettings;
  /** Fires after the backend has durably accepted the Turn, before model output. */
  onAccepted?: () => void;
}

export async function runAgentTurn({
  wfId,
  chatId,
  projectId,
  content,
  control,
  attachments,
  mode,
  surface,
  agentSurface,
  approvalMode = getApprovalMode(),
  agentSettings,
  onAccepted,
}: RunAgentTurnArgs): Promise<boolean> {
  const store = useChatStreamStore.getState();
  const ac = new AbortController();
  store.setAbort(ac, chatId);
  store.setLastInput({
    content,
    control,
    attachments,
    mode,
    surface,
    agentSurface,
    approvalMode,
    projectId,
  }, chatId);
  // Optimistic start: flip to `streaming` + drop the user's message bubble in
  // immediately (a fresh buffer for this turn) so the UI reacts the instant Send
  // is pressed — the user bubble + the "thinking" dots show right away instead
  // of waiting for the backend's `started` frame (which may lag, or never arrive
  // if the backend errors — in which case the catch below surfaces a toast). The
  // real `started` frame later only records the turnId (it must not clear this).
  store.beginTurn(chatId, '');
  if (!control) {
    store.appendChunk({ role: 'user', content, attachments }, chatId);
  }
  try {
    await streamAgentTurn({
      wfId,
      chatId,
      projectId,
      content,
      control,
      attachments,
      mode,
      surface,
      agentSurface,
      approvalMode,
      agentSettings,
      onAccepted,
      signal: ac.signal,
    });
    return true;
  } catch (e) {
    if ((e as { name?: string }).name === 'AbortError') {
      useChatStreamStore.getState().setState('interrupted', chatId);
    } else {
      toast.error(`Stream failed: ${errorMessage(e)}`);
      useChatStreamStore.getState().setState('failed', chatId);
    }
    return false;
  } finally {
    useChatStreamStore.getState().setAbort(null, chatId);
  }
}
