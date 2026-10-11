import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { cancelQueuedInput, fetchQueuedInput, queuedInputKey, type QueuedChatInput } from '@/lib/api/chat-queue';

interface Props {
  chatId: string;
  active: boolean;
  disabled: boolean;
  // The composer owns preparing the complete message, including its uploads.
  onQueue: (inputId: string) => Promise<QueuedChatInput | null>;
}

export function QueuedInputControl({ chatId, active, disabled, onQueue }: Props) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [attemptId, setAttemptId] = useState<string | null>(null);
  const key = queuedInputKey(chatId);
  const state = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchQueuedInput(chatId, signal), retry: false });
  const refresh = () => client.invalidateQueries({ queryKey: key });
  const submit = useMutation({
    retry: false,
    mutationFn: async () => {
      const inputId = crypto.randomUUID();
      setAttemptId(inputId);
      return onQueue(inputId);
    },
    onSuccess: receipt => {
      if (receipt) client.setQueryData(key, { input: receipt });
      setAttemptId(null);
      void refresh();
    },
    onError: () => { void refresh(); },
  });
  const cancel = useMutation({
    retry: false,
    mutationFn: (inputId: string) => cancelQueuedInput(chatId, inputId),
    onSuccess: receipt => { client.setQueryData(key, { input: receipt }); void refresh(); },
    onError: () => { void refresh(); },
  });
  const receipt = state.data?.input;
  const unconfirmed = submit.isError && attemptId !== receipt?.input_id;
  const pending = receipt && ['queued', 'claimed', 'blocked', 'unknown'].includes(receipt.status);
  const canCancel = receipt?.status === 'queued' || receipt?.status === 'blocked';
  return <div className="flex flex-wrap items-center gap-2 text-xs text-content-secondary" data-role="composer-queue">
    {active && <Button type="button" size="sm" variant="outline" data-action="agent-composer-queue"
      disabled={disabled || state.isPending || state.isError || !!pending || unconfirmed || submit.isPending}
      title={t('composer.queue.hint')} onClick={() => submit.mutate()}>
      {t('composer.queue.action')}
    </Button>}
    {unconfirmed && <span role="status">{t('composer.queue.unconfirmed')}</span>}
    {receipt && <div className="flex min-w-0 flex-1 items-start gap-1">
      <details className="min-w-0 flex-1" data-role="queued-input-details">
        <summary className="cursor-pointer rounded py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <span role="status">{t(`composer.queue.status.${receipt.status}`)}</span>
        </summary>
        <div className="max-h-28 space-y-1 overflow-y-auto break-words py-1">
          <p className="whitespace-pre-wrap">{receipt.message.content}</p>
          {receipt.message.attachments?.length ? <p>{t('composer.queue.attachments', { count: receipt.message.attachments.length })}</p> : null}
          {receipt.error_code && <p>{t(`composer.queue.error.${receipt.error_code}`, { defaultValue: receipt.error_code })}</p>}
        </div>
      </details>
      {canCancel && <Button type="button" size="icon" variant="ghost" className="size-7" disabled={cancel.isPending}
        title={t('composer.queue.cancel')} aria-label={t('composer.queue.cancel')}
        onClick={() => cancel.mutate(receipt.input_id)}><X className="size-3.5" /></Button>}
    </div>}
    {(state.isError || submit.isError || cancel.isError || receipt?.status === 'claimed' || receipt?.status === 'unknown') &&
      <Button type="button" size="sm" variant="ghost" disabled={state.isFetching}
        onClick={() => { void state.refetch(); }}>{t('composer.queue.check')}</Button>}
    {(state.error || cancel.error || (unconfirmed && submit.error)) &&
      <span role="alert">{(state.error || cancel.error || submit.error)?.message}</span>}
  </div>;
}
