import { CirclePause, CircleX, LoaderCircle, MessageCircle, MessageCircleQuestion } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ChatListItem } from '@/lib/api/queries/chats';
import { useChatStreamStore } from '@/stores/chat-stream';

type RunStatus = NonNullable<ChatListItem['latest_run']>['status'];

const appearance = {
  running: { Icon: LoaderCircle, className: 'text-state-running motion-safe:animate-spin', label: 'Running' },
  waiting_approval: { Icon: MessageCircleQuestion, className: 'text-state-warning', label: 'Needs your response' },
  cancel_requested: { Icon: CirclePause, className: 'text-state-warning', label: 'Stopping' },
  cancelled: { Icon: CirclePause, className: 'text-content-tertiary', label: 'Stopped' },
  failed: { Icon: CircleX, className: 'text-state-danger', label: 'Failed' },
} satisfies Record<Exclude<RunStatus, 'completed'>, { Icon: typeof CirclePause; className: string; label: string }>;

/** A row subscribes only to its status, not every token in every chat. */
function useRunStatus(chatId: string, run?: ChatListItem['latest_run']) {
  return useChatStreamStore(state => {
    const runtime = state.runtimes[chatId];
    if (runtime?.projectionActive) {
      if (run?.run_id === runtime.turnId && ['completed', 'cancelled', 'failed', 'cancel_requested'].includes(run.status)) return run.status;
      if (runtime.state === 'streaming') return runtime.waitingForUser ? 'waiting_approval' : 'running';
      // Only the same turn may replace its still-stale list snapshot.
      if (!run || run.run_id === runtime.turnId) {
        if (runtime.state === 'complete') return 'completed';
        if (runtime.state === 'cancelled') return 'cancelled';
        if (runtime.state === 'failed') return 'failed';
      }
    }
    return run?.status ?? null;
  });
}

export function ChatAliveIcon({ chatId, run, serving }: { chatId: string; run?: ChatListItem['latest_run']; serving: boolean }) {
  const status = useRunStatus(chatId, run);
  const alive = serving || status === 'running' || status === 'waiting_approval' || status === 'cancel_requested';
  return <MessageCircle data-chat-alive={alive} className={`size-5 shrink-0 ${alive ? 'text-state-success' : 'text-content-tertiary'}`} aria-hidden="true" />;
}

export function ChatRunStatus({ chatId, run }: { chatId: string; run?: ChatListItem['latest_run'] }) {
  const { t } = useTranslation();
  const status = useRunStatus(chatId, run);
  if (!status || status === 'completed') return null;
  const { Icon, className, label } = appearance[status];
  const title = t(`chat.runStatus.${status}`, label);
  return <span className="inline-flex shrink-0" title={title} role="img" aria-label={title} data-chat-run-status={status}>
    <Icon className={`size-3.5 ${className}`} aria-hidden="true" />
  </span>;
}
