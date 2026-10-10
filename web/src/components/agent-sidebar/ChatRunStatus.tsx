import { CircleCheck, CirclePause, CircleX, LoaderCircle, MessageCircleQuestion } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ChatListItem } from '@/lib/api/queries/chats';
import { useChatStreamStore } from '@/stores/chat-stream';

type RunStatus = NonNullable<ChatListItem['latest_run']>['status'];

const appearance = {
  running: { Icon: LoaderCircle, className: 'text-state-running motion-safe:animate-spin', label: 'Running' },
  waiting_approval: { Icon: MessageCircleQuestion, className: 'text-state-warning', label: 'Needs your response' },
  cancel_requested: { Icon: CirclePause, className: 'text-state-warning', label: 'Stopping' },
  completed: { Icon: CircleCheck, className: 'text-content-tertiary', label: 'Completed' },
  cancelled: { Icon: CirclePause, className: 'text-content-tertiary', label: 'Stopped' },
  failed: { Icon: CircleX, className: 'text-state-danger', label: 'Failed' },
} satisfies Record<RunStatus, { Icon: typeof CircleCheck; className: string; label: string }>;

/** A row subscribes only to its status, not every token in every chat. */
export function ChatRunStatus({ chatId, run }: { chatId: string; run?: ChatListItem['latest_run'] }) {
  const { t } = useTranslation();
  const status = useChatStreamStore(state => {
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
  if (!status) return null;
  const { Icon, className, label } = appearance[status];
  const unread = status === 'completed' && run?.status === 'completed' && run.unread;
  const title = unread ? t('chat.runStatus.unread', 'Completed · unread') : t(`chat.runStatus.${status}`, label);
  return <span className="inline-flex shrink-0" title={title} role="img" aria-label={title} data-chat-run-status={status} data-chat-unread={unread || undefined}>
    <Icon className={`size-3.5 ${unread ? 'text-state-info' : className}`} aria-hidden="true" />
  </span>;
}
