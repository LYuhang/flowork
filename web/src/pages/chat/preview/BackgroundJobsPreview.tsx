import { PreviewContextMenu } from './PreviewContextMenu';
import { useCallback, useMemo, useState } from 'react';
import {
  Ban,
  CheckCircle2,
  ChevronDown,
  Clock3,
  LoaderCircle,
  Square,
  Workflow,
  XCircle,
} from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { ExecutionThread } from '@/components/presentation/ExecutionThread';
import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import { StatusDot, type SemanticStatus } from '@/components/ui/status';
import {
  cancelBackgroundJob,
  type BackgroundJob,
  type BackgroundJobFilter,
  useBackgroundJobs,
  useReferencedBackgroundJob,
} from '@/lib/api/queries/chats';
import {
  type BackgroundJobEvent,
  useBackgroundJobEvents,
} from '@/lib/api/sse/background-job-events';
import { cn } from '@/lib/utils';

const FILTERS: BackgroundJobFilter[] = ['current', 'all', 'active', 'completed', 'failed', 'cancelled'];

const ACTIVE = new Set<BackgroundJob['status']>([
  'queued',
  'running',
  'cancelling',
]);

function tone(status: BackgroundJob['status']): SemanticStatus {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'cancelled') return 'neutral';
  if (status === 'cancelling') return 'warning';
  return 'running';
}

function timestamp(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function BackgroundJobsPreview({
  initialJobId,
  deliveryBatchId,
  ...props
}: {
  scopeId: string;
  chatId: string;
  initialJobId?: string;
  deliveryBatchId?: string;
  onOpenFile?: (path: string) => void;
}) {
  return (
    <BackgroundJobsPreviewContent
      key={`${initialJobId ?? ''}:${deliveryBatchId ?? ''}`}
      initialJobId={initialJobId}
      deliveryBatchId={deliveryBatchId}
      {...props}
    />
  );
}

function BackgroundJobsPreviewContent({
  scopeId,
  chatId,
  initialJobId,
  deliveryBatchId,
  onOpenFile,
}: {
  scopeId: string;
  chatId: string;
  initialJobId?: string;
  deliveryBatchId?: string;
  onOpenFile?: (path: string) => void;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<BackgroundJobFilter>(
    deliveryBatchId || initialJobId ? 'all' : 'current',
  );
  const [selectedJobId, setSelectedJobId] = useState<string | null>(
    initialJobId ?? null,
  );
  const [batchFilter, setBatchFilter] = useState(deliveryBatchId ?? '');
  const [confirmCancelId, setConfirmCancelId] = useState<string | null>(null);
  const [eventsByJob, setEventsByJob] = useState<
    Record<string, BackgroundJobEvent[]>
  >({});
  const jobsQuery = useBackgroundJobs(scopeId, chatId, filter);
  const referencedJob = useReferencedBackgroundJob(scopeId, chatId, initialJobId);
  const queryClient = useQueryClient();
  const cancel = useMutation({
    mutationFn: (jobId: string) => cancelBackgroundJob(scopeId, chatId, jobId),
    onSuccess: async () => {
      setConfirmCancelId(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['background-jobs', scopeId, chatId] }),
        queryClient.invalidateQueries({ queryKey: ['chat-state', scopeId, chatId] }),
      ]);
    },
  });
  const jobs = useMemo(() => {
    const recent = jobsQuery.data ?? [];
    const referenced = referencedJob.data;
    if (!referenced) return recent;
    return [referenced, ...recent.filter(job => job.job_id !== referenced.job_id)];
  }, [jobsQuery.data, referencedJob.data]);
  const onJobEvent = useCallback((event: BackgroundJobEvent) => {
    setEventsByJob((current) => {
      const previous = current[event.job_id] ?? [];
      if (previous.some((item) => item.event_id === event.event_id)) {
        return current;
      }
      return {
        ...current,
        [event.job_id]: [...previous, event].sort(
          (left, right) => left.event_id - right.event_id,
        ),
      };
    });
    void queryClient.invalidateQueries({
      queryKey: ['background-jobs', scopeId, chatId],
    });
  }, [chatId, queryClient, scopeId]);
  useBackgroundJobEvents(scopeId, chatId, onJobEvent);
  const visibleJobs = batchFilter
    ? jobs.filter((job) => job.delivery_batch_id === batchFilter)
    : jobs;
  const summary = useMemo(() => ({
    active: jobs.filter((job) => ACTIVE.has(job.status)).length,
    completed: jobs.filter((job) => job.status === 'completed').length,
    issues: jobs.filter((job) => job.status === 'failed' || job.status === 'cancelled').length,
  }), [jobs]);

  if (jobsQuery.isLoading || (initialJobId && referencedJob.isLoading)) {
    return <AsyncState kind="loading" title={t('chat.background.loading', 'Loading background tasks…')} className="h-full rounded-none border-0" />;
  }
  if (jobsQuery.isError || referencedJob.isError) {
    return (
      <AsyncState
        kind="error"
        title={t('chat.background.loadError', 'Unable to load background tasks')}
        description={t('chat.background.loadErrorHint', 'Check the connection and refresh this view.')}
        actionLabel={t('refresh', 'Refresh')}
        onAction={() => { void jobsQuery.refetch(); if (initialJobId) void referencedJob.refetch(); }}
        className="h-full rounded-none border-0"
      />
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-work">
      {initialJobId && !jobs.some(job => job.job_id === initialJobId) && (
        <div role="status" className="px-3 py-2 text-xs text-muted-foreground">
          {t('chat.background.sourceUnavailable', 'The referenced task is unavailable or you no longer have access.')}
        </div>
      )}
      <div className="grid shrink-0 grid-cols-3 gap-2 border-b border-edge-subtle p-3">
        {[
          { label: t('chat.background.summary.active', 'Active'), value: summary.active, icon: LoaderCircle },
          { label: t('chat.background.summary.completed', 'Completed'), value: summary.completed, icon: CheckCircle2 },
          { label: t('chat.background.summary.other', 'Other'), value: summary.issues, icon: Clock3 },
        ].map(({ label, value, icon: Icon }) => (
          <div key={label} className="rounded-lg border border-edge-subtle bg-surface-raised px-3 py-2">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Icon className="h-3.5 w-3.5" />
              {label}
            </div>
            <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
          </div>
        ))}
      </div>
      <div className="app-scrollbar flex shrink-0 gap-1 overflow-x-auto border-b border-edge-subtle px-3 py-2">
        {FILTERS.map((filterValue) => (
          <button
            key={filterValue}
            type="button"
            onClick={() => {
              setFilter(filterValue);
              void queryClient.invalidateQueries({
                queryKey: ['background-jobs', scopeId, chatId],
              });
            }}
            className={cn(
              'rounded-full px-3 py-1 text-xs transition-colors',
              filter === filterValue
                ? 'bg-foreground text-background'
                : 'bg-surface-sunken text-muted-foreground hover:text-foreground',
            )}
          >
            {t(`chat.background.filter.${filterValue}`, filterValue[0].toUpperCase() + filterValue.slice(1))}
          </button>
        ))}
      </div>
      <div className="app-scrollbar min-h-0 flex-1 overflow-y-auto p-3">
        {batchFilter ? (
          <div className="mb-2 flex items-center justify-between rounded-md border border-edge-subtle bg-surface-sunken px-3 py-2 text-xs text-muted-foreground">
            <span>{t('chat.background.deliveryBatch', 'Delivery batch · {{count}} tasks', { count: visibleJobs.length })}</span>
            <button
              type="button"
              className="text-foreground hover:underline"
              onClick={() => {
                setBatchFilter('');
                void queryClient.invalidateQueries({
                  queryKey: ['background-jobs', scopeId, chatId],
                });
              }}
            >
              {t('chat.background.viewAll', 'View all')}
            </button>
          </div>
        ) : null}
        {visibleJobs.length === 0 ? (
          <CompactEmptyState
            icon={Workflow}
            title={t('chat.background.empty', 'No background tasks in this view.')}
            className="h-full border-0 bg-transparent"
          />
        ) : (
          <div className="space-y-2">
            {visibleJobs.map((job) => {
              const total = job.progress.total;
              const progress = total && total > 0
                ? Math.min(100, Math.round((job.progress.current / total) * 100))
                : null;
              return (
                <PreviewContextMenu key={job.job_id}
                  label={t('preview.reference.job', 'Quote this task')}
                  build={() => ({ schema_version: 1, id: crypto.randomUUID(), type: 'resource', label: job.title || job.tool_name,
                    resource: { kind: 'job', chat_id: chatId, job_id: job.job_id } })}>
                <details
                  open={selectedJobId === job.job_id}
                  onToggle={(event) => {
                    if (event.currentTarget.open) {
                      setSelectedJobId(job.job_id);
                    } else if (selectedJobId === job.job_id) {
                      setSelectedJobId(null);
                    }
                  }}
                  className="group rounded-lg border border-edge-subtle bg-surface-raised"
                >
                  <summary className="flex cursor-pointer list-none items-start gap-2 p-3">
                    <StatusDot className="mt-1.5" status={tone(job.status)} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium" title={job.title}>
                        {job.title || job.tool_name}
                      </div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                        <span className="font-mono" title={job.job_id}>{job.job_id.slice(0, 8)}</span>
                        <span>
                          {job.delivery_status === 'delivered'
                            ? t('chat.background.status.delivered', 'delivered')
                            : job.progress.message || t(`chat.background.status.${job.status}`, job.status.replace('_', ' '))}
                        </span>
                        {progress !== null ? <span>{progress}%</span> : null}
                      </div>
                      {progress !== null ? (
                        <div className="mt-2 h-1 overflow-hidden rounded-full bg-surface-sunken">
                          <div className="h-full bg-focus" style={{ width: `${progress}%` }} />
                        </div>
                      ) : null}
                    </div>
                    {ACTIVE.has(job.status) ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        disabled={job.status === 'cancelling' || cancel.isPending}
                        onClick={(event) => {
                          event.preventDefault();
                          event.stopPropagation();
                          setSelectedJobId(job.job_id);
                          setConfirmCancelId(job.job_id);
                        }}
                        aria-label={t(
                          'chat.background.cancelAria',
                          'Cancel {{id}}',
                          { id: job.job_id },
                        ).replace('{{id}}', job.job_id)}
                      >
                        <Square className="h-3 w-3" />
                      </Button>
                    ) : job.status === 'completed' ? (
                      <CheckCircle2 className="mt-0.5 h-4 w-4 text-state-success" />
                    ) : job.status === 'failed' ? (
                      <XCircle className="mt-0.5 h-4 w-4 text-state-danger" />
                    ) : (
                      <Ban className="mt-0.5 h-4 w-4 text-muted-foreground" />
                    )}
                    <ChevronDown className="mt-0.5 h-4 w-4 text-muted-foreground transition-transform group-open:rotate-180" />
                  </summary>
                  {confirmCancelId === job.job_id ? (
                    <div className="flex items-center justify-end gap-2 border-t border-edge-subtle bg-surface-sunken px-3 py-2 text-xs">
                      <span className="mr-auto text-muted-foreground">{t('chat.background.cancelConfirm', 'Cancel this task?')}</span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => setConfirmCancelId(null)}
                      >
                        {t('chat.background.keepRunning', 'Keep running')}
                      </Button>
                      <Button
                        type="button"
                        variant="destructive"
                        size="sm"
                        disabled={cancel.isPending}
                        onClick={() => cancel.mutate(job.job_id)}
                      >
                        {t('chat.background.cancelTask', 'Cancel task')}
                      </Button>
                    </div>
                  ) : null}
                  <div className="border-t border-edge-subtle px-3 py-2 text-xs">
                    <dl className="grid grid-cols-[6rem_minmax(0,1fr)] gap-x-2 gap-y-1 text-muted-foreground">
                      <dt>{t('chat.background.created', 'Created')}</dt><dd>{timestamp(job.created_at)}</dd>
                      <dt>{t('chat.background.finished', 'Finished')}</dt><dd>{timestamp(job.finished_at)}</dd>
                      <dt>{t('chat.background.delivery', 'Delivery')}</dt><dd>{job.delivery_status}</dd>
                      <dt>{t('chat.background.delivered', 'Delivered')}</dt><dd>{timestamp(job.delivered_at)}</dd>
                      {job.result_ref ? (
                        <>
                          <dt>{t('chat.background.resultFile', 'Result file')}</dt>
                          <dd>
                            <button
                              type="button"
                              className="break-all text-left font-mono text-focus hover:underline"
                              onClick={() => onOpenFile?.(job.result_ref!)}
                            >
                              {job.result_ref}
                            </button>
                          </dd>
                        </>
                      ) : null}
                    </dl>
                    {Object.keys(job.input).length > 0 ? (
                      <>
                        <div className="mt-3 font-medium text-foreground">{t('chat.background.task', 'Task')}</div>
                        <pre className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-surface-sunken p-2 text-xs text-foreground">
                          {JSON.stringify(job.input, null, 2)}
                        </pre>
                      </>
                    ) : null}
                    {Object.keys(job.result).length > 0 ? (
                      <>
                        <div className="mt-3 font-medium text-foreground">{t('chat.background.result', 'Result')}</div>
                        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-surface-sunken p-2 text-xs text-foreground">
                          {JSON.stringify(job.result, null, 2)}
                        </pre>
                      </>
                    ) : null}
                    {Object.keys(job.error).length > 0 ? (
                      <>
                        <div className="mt-3 font-medium text-state-danger">{t('common.error', 'Error')}</div>
                        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-state-danger/10 p-2 text-xs text-state-danger">
                          {JSON.stringify(job.error, null, 2)}
                        </pre>
                      </>
                    ) : null}
                    {(eventsByJob[job.job_id]?.length ?? 0) > 0 ? (
                      <div className="mt-3">
                        <div className="font-medium text-foreground">{t('chat.background.events', 'Events')}</div>
                        <ExecutionThread
                          className="mt-2"
                          items={eventsByJob[job.job_id].map((event, index, events) => ({
                            id: String(event.event_id),
                            title: event.event_type.replaceAll('_', ' '),
                            meta: timestamp(event.created_at),
                            status: index === events.length - 1 && ACTIVE.has(job.status)
                              ? 'running'
                              : job.status === 'failed'
                                ? 'danger'
                                : 'success',
                          }))}
                        />
                      </div>
                    ) : null}
                  </div>
                </details>
                </PreviewContextMenu>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
