import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useFormatDateTime } from '@/lib/timezone';
import { Button } from '@/components/ui/button';
import { taskResultRequest, type EvaluationConfig, type EvaluationState } from '@/lib/api/tasks';
import { EvaluationEditor } from './EvaluationEditor';

export function EvaluationTab({ taskId, canEdit, canExecute }: { taskId: string; canEdit: boolean; canExecute: boolean }) {
  const { t } = useTranslation();
  const formatDateTime = useFormatDateTime();
  const client = useQueryClient();
  const [draft, setDraft] = useState<EvaluationConfig | null>(null);
  const query = useQuery({ queryKey: ['evaluation', taskId], queryFn: () => taskResultRequest<EvaluationState>(taskId, 'evaluation'),
    refetchInterval: 3000 });
  const value = draft ?? query.data?.config ?? { enabled: false, script: '' };
  const active = query.data?.records.some(r => r.status === 'queued' || r.status === 'running');
  const mutation = useMutation({ mutationFn: async (run: boolean) => {
    if (canEdit) await taskResultRequest(taskId, 'evaluation', 'PUT', value);
    if (run) await taskResultRequest(taskId, 'evaluation', 'POST');
  }, onSuccess: async () => { setDraft(null); await client.invalidateQueries({ queryKey: ['evaluation', taskId] }); },
  onError: (error: Error) => toast.error(error.message) });
  if (query.isPending) return <p className="p-4">{t('evaluation.loading', 'Loading…')}</p>;
  if (query.error) return <p role="alert" className="p-4 text-destructive">{query.error.message}</p>;
  return <div className="space-y-6 p-4">
    <EvaluationEditor value={value} onChange={setDraft} disabled={!canEdit || mutation.isPending} alwaysOpen />
    <div className="flex flex-wrap items-center gap-3">
      {canEdit && <Button variant="outline" disabled={mutation.isPending || (value.enabled && !value.script.trim())} onClick={() => mutation.mutate(false)}>{t('evaluation.save', 'Save script')}</Button>}
      {canExecute && <Button disabled={!query.data?.ready || active || mutation.isPending || !value.script.trim()} onClick={() => mutation.mutate(true)}>{active ? t('evaluation.running', 'Evaluating…') : t('evaluation.run', 'Evaluate')}</Button>}
      {!query.data?.ready && <p className="text-sm text-muted-foreground">{t('evaluation.notReady', 'Evaluation becomes available after inference results are saved.')}</p>}
    </div>
    <h3 className="font-medium">{t('evaluation.history', 'Evaluation history')}</h3>
    {!query.data?.records.length && <p className="text-sm text-muted-foreground">{t('evaluation.empty', 'No evaluations yet.')}</p>}
    {query.data?.records.map(record => <article key={record.id} className="space-y-3 rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">{t(`evaluation.${record.status}`, record.status)}</span>
        <time className="text-xs text-muted-foreground">{formatDateTime(record.created_at)}</time>
      </div>
      <p className="text-xs text-muted-foreground">{query.data?.result_version && record.result_version !== query.data.result_version && <strong className="mr-2 text-amber-600">{t('evaluation.previousVersion', 'Earlier result version')}</strong>}{t('evaluation.version', 'Result version')}: <code title={record.result_version}>{record.result_version.slice(0, 12)}</code> · {record.row_count} {t('evaluation.rows', 'rows')} {record.partial && `· ${t('evaluation.partial', 'Partial results')}`}</p>
      {record.error && <p role="alert" className="break-words text-sm text-destructive">{record.error}</p>}
      {record.metrics && <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{Object.entries(record.metrics).map(([key, metric]) => <div key={key} className="min-w-0 rounded-md bg-muted/40 p-3"><dt className="break-all text-xs text-muted-foreground">{key}</dt><dd className="mt-1 whitespace-pre-wrap break-all font-mono text-lg">{typeof metric === 'object' ? JSON.stringify(metric, null, 2) : String(metric)}</dd></div>)}</dl>}
      <details><summary className="cursor-pointer text-sm">{t('evaluation.scriptSnapshot', 'Script used for this evaluation')}</summary><pre className="mt-2 overflow-auto rounded bg-muted p-3 text-xs">{record.script}</pre></details>
    </article>)}
  </div>;
}
