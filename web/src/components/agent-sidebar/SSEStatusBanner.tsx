/** Explicit actions for failed/interrupted turns. Retrying starts a new turn;
 * cancellation always uses the backend stop protocol. */
import { useTranslation } from 'react-i18next';
import { useChatStreamStore } from '@/stores/chat-stream';
import { Button } from '@/components/ui/button';
import { cancelActiveTurn } from '@/lib/api/cancel-turn';
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

  const canRetry = (!!lastInput?.content || !!lastInput?.attachments?.length || !!lastInput?.control) && !!chatId;

  const handleRetry = () => {
    if (!canRetry || !chatId || !lastInput) return;
    void runTurn({
      wfId,
      chatId,
      projectId: lastInput.projectId,
      content: lastInput.content,
      skillUse: lastInput.skillUse,
      control: lastInput.control,
      attachments: lastInput.attachments,
      mode: lastInput.mode,
      surface: lastInput.surface,
      agentSurface: lastInput.agentSurface,
      approvalMode: lastInput.approvalMode,
    });
  };

  const handleCancel = () => {
    if (chatId) void cancelActiveTurn(chatId);
  };

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-2 border-b border-destructive/40 bg-destructive/10 p-2 text-xs"
    >
      <span className="flex-1">
        {statusText}
        <span className="mt-1 block text-xs">
          {t('sse_retry_request_hint', 'Retry starts a new execution of your previous request; it does not resume the connection.')}
        </span>
      </span>
      <Button
        size="sm"
        variant="ghost"
        onClick={handleRetry}
        disabled={!canRetry}
        aria-label={t('sse_retry_request', 'Retry last request')}
      >
        {t('sse_retry_request', 'Retry last request')}
      </Button>
      {state === 'interrupted' && <Button
        size="sm"
        variant="ghost"
        onClick={handleCancel}
        aria-label={t('sse_cancel_turn', 'Cancel turn')}
      >
        {t('sse_cancel_turn', 'Cancel turn')}
      </Button>}
    </div>
  );
}
