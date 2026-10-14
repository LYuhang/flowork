import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { CornerDownRight, Loader2, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { fetchInputControls, inputControlsKey, submitChatInput } from '@/lib/api/chat-inputs';

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
    onSuccess: async (_receipt, input) => {
      onSubmitted(input.text);
      // The POST response describes submission, not the latest receipt. An
      // acknowledgment (or another window's input) may already be newer.
      await client.invalidateQueries({ queryKey: inputControlsKey(chatId, runId) });
    },
    // A failed HTTP response may have arrived after the server committed.
    // Re-read its receipt; never automatically send the text again.
    onError: () => { void client.invalidateQueries({ queryKey: inputControlsKey(chatId, runId) }); },
  });
  const receipt = controls.data?.latest_input;
  const recoveredText = submit.isError && receipt?.status === 'accepted'
    && receipt.input_id === submit.variables?.input_id ? submit.variables.text : null;
  const resetSubmission = submit.reset;
  useEffect(() => {
    if (recoveredText === null) return;
    // A lost POST response is no longer uncertain after this input's receipt
    // confirms acceptance. The composer preserves any subsequently edited draft.
    onSubmitted(recoveredText);
    resetSubmission();
  }, [recoveredText, onSubmitted, resetSubmission]);
  const statusUnavailable = controls.isError && submit.isSuccess;
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
    <div className="contents text-xs text-content-secondary" data-role="composer-steering">
      {active && runId && supported && (
        <Button type="button" size="sm" variant="outline" className="h-7 shrink-0 gap-1 px-2 text-xs"
          disabled={disabled || controls.isError || !text.trim() || submit.isPending || !controls.data?.available_controls.includes('steer')}
          onClick={() => submit.mutate({ input_id: crypto.randomUUID(), run_id: runId, text })} data-action="agent-composer-steer"
          title={t('composer.steer.hint')}>
          {submit.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <CornerDownRight className="size-3.5" />}
          {t('composer.steer.action')}
        </Button>
      )}
      {(receipt || submit.isError || statusUnavailable) && <span className="order-last basis-full" role="status">
        {statusUnavailable ? t('composer.steer.statusUnavailable') : unconfirmed ? t('composer.steer.checkStatus') : receiptLabel}
      </span>}
      {(statusUnavailable || submit.isError || receipt?.status === 'unknown' || receipt?.status === 'submitting') && (
        <Button type="button" size="sm" variant="ghost" disabled={controls.isFetching}
          onClick={() => { void controls.refetch(); }} aria-label={t('composer.steer.refresh')} title={t('composer.steer.refresh')}>
          <RefreshCw className="size-3.5" />
        </Button>
      )}
    </div>
  );
}
