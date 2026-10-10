import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CornerDownRight, Loader2, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { fetchInputControls, inputControlsKey, submitChatInput, type ChatInputControls } from '@/lib/api/chat-inputs';

interface Props {
  chatId: string;
  runId: string | null;
  text: string;
  active: boolean;
  disabled: boolean;
  onSubmitted: (text: string) => void;
}

export function SteerInputControl({ chatId, runId, text, active, disabled, onSubmitted }: Props) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const key = [...inputControlsKey(chatId, runId), active];
  const controls = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => fetchInputControls(chatId, runId, signal),
    retry: false,
    refetchOnWindowFocus: true,
  });
  const submit = useMutation({
    retry: false,
    mutationFn: (input: { input_id: string; run_id: string; text: string }) => submitChatInput(chatId, input),
    onSuccess: (receipt, input) => {
      client.setQueryData<ChatInputControls>(key, current => current && ({ ...current, latest_input: receipt, available_controls: [] }));
      onSubmitted(input.text);
      void client.invalidateQueries({ queryKey: inputControlsKey(chatId, runId) });
    },
    // A failed HTTP response may have arrived after the server committed.
    // Re-read its receipt; never automatically send the text again.
    onError: () => { void client.invalidateQueries({ queryKey: inputControlsKey(chatId, runId) }); },
  });
  const receipt = controls.data?.latest_input;
  const unconfirmed = submit.isError && receipt?.input_id !== submit.variables?.input_id;
  const receiptLabel = receipt ? {
    submitting: t('composer.steer.status.submitting'),
    accepted: t('composer.steer.status.accepted'),
    rejected: t('composer.steer.status.rejected'),
    unknown: t('composer.steer.status.unknown'),
  }[receipt.status] : '';
  const supported = controls.data?.supported_controls.includes('steer');
  if (!supported && !receipt && !submit.isError) return null;
  return (
    <div className="flex max-w-full flex-wrap items-center gap-2 text-xs text-content-secondary" data-role="composer-steering">
      {active && runId && supported && (
        <Button type="button" size="sm" variant="outline"
          disabled={disabled || !text.trim() || submit.isPending || !controls.data?.available_controls.includes('steer')}
          onClick={() => submit.mutate({ input_id: crypto.randomUUID(), run_id: runId, text })} data-action="agent-composer-steer"
          title={t('composer.steer.hint')}>
          {submit.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <CornerDownRight className="size-3.5" />}
          {t('composer.steer.action')}
        </Button>
      )}
      {(receipt || submit.isError) && <span role="status">
        {unconfirmed ? t('composer.steer.checkStatus') : receiptLabel}
      </span>}
      {(submit.isError || receipt?.status === 'unknown' || receipt?.status === 'submitting') && (
        <Button type="button" size="sm" variant="ghost" disabled={controls.isFetching}
          onClick={() => { void controls.refetch(); }} aria-label={t('composer.steer.refresh')} title={t('composer.steer.refresh')}>
          <RefreshCw className="size-3.5" />
        </Button>
      )}
    </div>
  );
}
