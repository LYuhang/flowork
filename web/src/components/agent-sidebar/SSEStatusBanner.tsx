/**
 * SSE reconnect banner for the agent chat sidebar.
 *
 * Rendered inline at the top of {@link AgentChatSidebar} and visible
 * when the chat-stream store is in `'interrupted'` (the browser lost its
 * resumable SSE transport) or `'failed'` (the backend Runtime/model ended the
 * Turn with an error). Those states deliberately use different copy: a model
 * provider failure is not a browser reconnection failure. User-requested Stop
 * has its own explicit `'cancelled'` state and composer Retry action.
 *
 * Three affordances appear when a stream disconnects:
 *   - **Retry** — re-fires the same turn via `runAgentTurn` using the
 *     `lastInput` stashed before the previous send. Disabled when no
 *     `lastInput` is captured (e.g. banner re-rendered after a reset).
 *   - **Cancel turn** — clears the disconnected state and resets the
 *     in-flight controller, returning to `idle` so the next Send
 *     starts a clean turn. Also fires `abortController.abort()` if a
 *     controller is somehow still held (defensive — should be null
 *     by the time the banner is visible, but cheap insurance).
 *   - **Dismiss** is folded into Cancel turn because a dismiss-only banner
 *     could hide a still-running server task.
 *
 * Why a banner rather than a toast: a toast is ephemeral and easy to
 * miss, but a disconnected SSE stream is sticky state the user needs
 * to see while deciding whether to retry. Matches how Linear / Cursor
 * surface "Reconnecting…" inline above their chat panes.
 */
import { useTranslation } from 'react-i18next';
import { useChatStreamStore } from '@/stores/chat-stream';
import { Button } from '@/components/ui/button';
import { runAgentTurn } from '@/lib/api/sse/run-agent-turn';

export interface SSEStatusBannerProps {
  wfId: string;
  activeChatId?: string | null;
  runTurn?: typeof runAgentTurn;
}

export function SSEStatusBanner({
  wfId,
  activeChatId,
  runTurn = runAgentTurn,
}: SSEStatusBannerProps) {
  const { t } = useTranslation();
  const runtime = useChatStreamStore((s) =>
    activeChatId
      ? s.runtimes[activeChatId] ??
        (s.chatId === activeChatId
          ? {
              chatId: activeChatId,
              turnId: s.turnId,
              state: s.state,
              buffer: s.buffer,
              messages: s.messages,
              todoItems: s.todoItems,
              abortController: s.abortController,
              lastInput: s.lastInput,
            }
          : undefined)
      : s.chatId
        ? s.runtimes[s.chatId]
        : undefined,
  );
  const state = runtime?.state ?? 'idle';
  const lastInput = runtime?.lastInput ?? null;
  const chatId = runtime?.chatId ?? null;
  if (state !== 'interrupted' && state !== 'failed') return null;

  const statusText = state === 'interrupted'
    ? t(
        'sse_disconnected',
        'Connection interrupted. The live response could not be resumed.',
      )
    : t(
        'agent_run_failed',
        'The agent run failed before completion.',
      );

  const canRetry = (!!lastInput?.content || !!lastInput?.control) && !!chatId;

  const handleRetry = () => {
    if (!canRetry || !chatId || !lastInput) return;
    void runTurn({
      wfId,
      chatId,
      projectId: lastInput.projectId,
      content: lastInput.content,
      control: lastInput.control,
      attachments: lastInput.attachments,
      mode: lastInput.mode,
      surface: lastInput.surface,
      agentSurface: lastInput.agentSurface,
      approvalMode: lastInput.approvalMode,
    });
  };

  const handleCancel = () => {
    const s = useChatStreamStore.getState();
    if (!chatId) return;
    s.runtimes[chatId]?.abortController?.abort();
    s.setAbort(null, chatId);
    s.setState('idle', chatId);
  };

  return (
    <div
      role="status"
      className="flex items-center gap-2 border-b border-destructive/40 bg-destructive/10 p-2 text-xs"
    >
      <span className="flex-1">
        {statusText}
      </span>
      <Button
        size="sm"
        variant="ghost"
        onClick={handleRetry}
        disabled={!canRetry}
        aria-label={t('sse_retry', 'Retry')}
      >
        {t('sse_retry', 'Retry')}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={handleCancel}
        aria-label={t('sse_cancel_turn', 'Cancel turn')}
      >
        {t('sse_cancel_turn', 'Cancel turn')}
      </Button>
    </div>
  );
}
