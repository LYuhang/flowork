import { ScheduledRunCreatePanel } from './ScheduledRunCreatePanel';
import { lazy, Suspense, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { toast } from 'sonner';
import {
  CalendarClock,
  ChevronDown,
  Download,
  ListFilter,
  MoreHorizontal,
  Play,
  RefreshCw,
  Search,
  Share2,
  X,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import type { SearchSelectOption } from '@/components/ui/search-select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Skeleton } from '@/components/ui/skeleton';
import {
  cancelTask,
  getTaskSummary,
  listTasks,
  pauseScheduledRun,
  resumeTask,
  resumeScheduledRun,
  runScheduledNow,
  type Task,
  type TaskSandboxStatus,
  type TaskStatus,
  type TaskType,
} from '@/lib/api/tasks';
import { useWorkspaceList } from '@/lib/api/queries/workflows';
import { TaskWorkflowVersion } from './TaskWorkflowVersion';
import { useTaskWorkflowVersion } from './useTaskWorkflowVersion';
import { useFormatDateTime } from '@/lib/timezone';
import { ManagementPageShell, ManagementToolbar } from '@/components/layout/management-page-shell';
import { OperationalSummary } from '@/components/layout/operational-summary';
import { ResourceShareDialog } from '@/components/modals/ResourceShareDialog';
import { ProgressState, StatusBadge, StatusDot, type SemanticStatus } from '@/components/ui/status';
import { ActionableError } from '@/components/presentation/ActionableError';
import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { AsyncState } from '@/components/ui/async-state';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import { SharedResourceList } from '@/components/resources/SharedResourceList';
import {
  ResourceScopeSwitch,
  type ResourceListScope,
} from '@/components/resources/ResourceScopeSwitch';

const PAGE_SIZE = 25;
const TASK_LIST_REFETCH_ACTIVE_MS = 2_000;
const TASK_LIST_REFETCH_IDLE_MS = 5_000;
const BATCH_STATUS_OPTIONS: TaskStatus[] = [
  'queued',
  'running',
  'resuming',
  'cancelling',
  'failed',
  'interrupted',
  'finished',
  'finished_with_errors',
  'cancelled',
];
const SCHEDULED_STATUS_OPTIONS: TaskStatus[] = [
  'queued',
  'finished',
  'enabled',
  'paused',
  'running',
  'failed',
  'cancelled',
];
const CANCELLABLE: TaskStatus[] = ['queued', 'running', 'resuming'];
const RESUMABLE: TaskStatus[] = ['cancelled', 'failed', 'interrupted', 'finished_with_errors'];
const ACTIVE_TASK_STATUSES: TaskStatus[] = ['queued', 'running', 'resuming', 'cancelling'];

// These controls are only visible after the user opens a task-creation
// surface. Keep their search/canvas/file tooling out of the task-list
// navigation path; behind a high-latency proxy those transitive chunks were
// adding a large request waterfall before the list could become interactive.
const SearchSelect = lazy(() =>
  import('@/components/ui/search-select').then((module) => ({
    default: module.SearchSelect,
  })),
);
const BatchTab = lazy(() =>
  import('@/pages/canvas/inspector/BatchTab').then((module) => ({
    default: module.BatchTab,
  })),
);

function DeferredControlFallback({ className = 'h-10 w-full' }: { className?: string }) {
  return <Skeleton className={className} />;
}
const ACTIVE_RANK: Record<TaskStatus, number> = {
  running: 0,
  resuming: 0,
  cancelling: 0,
  queued: 0,
  failed: 1,
  interrupted: 1,
  finished_with_errors: 1,
  finished: 2,
  cancelled: 3,
  enabled: 2,
  paused: 3,
};

function taskSemanticStatus(status: TaskStatus): SemanticStatus {
  switch (status) {
    case 'queued':
      return 'info';
    case 'running':
    case 'resuming':
      return 'running';
    case 'cancelling':
      return 'warning';
    case 'finished':
    case 'enabled':
      return 'success';
    case 'finished_with_errors':
    case 'interrupted':
      return 'warning';
    case 'failed':
      return 'danger';
    case 'cancelled':
    case 'paused':
      return 'neutral';
  }
}

/** Render cancellation/cleanup as one simple user-facing state. */
function visibleTaskStatus(status: TaskStatus): TaskStatus {
  return status === 'cancelling' || status === 'interrupted'
    ? 'cancelled'
    : status;
}

function formatScheduleProgress(
  task: Task,
  formatTime: (value?: string | null) => string,
  t: TFunction,
): string {
  const payload = task.payload && typeof task.payload === 'object'
    ? task.payload as Record<string, unknown>
    : {};
  const next = typeof payload.next_run_at === 'string' ? payload.next_run_at : null;
  const last = typeof payload.last_status === 'string' ? payload.last_status : null;
  if (task.status === 'paused') return t('tasks.scheduleProgress.paused', 'Paused');
  if (Number(payload.running_count ?? 0) || Number(payload.queued_count ?? 0)) {
    return t('tasks.scheduled.activeSummary', '{{running}} running · {{queued}} queued', {running: Number(payload.running_count ?? 0), queued: Number(payload.queued_count ?? 0)});
  }
  if (task.status === 'running') {
    return last
      ? t('tasks.scheduleProgress.runningWithLast', 'Running · last {{status}}', { status: last })
      : t('tasks.scheduleProgress.running', 'Running');
  }
  if (next) return t('tasks.scheduleProgress.next', 'Next: {{time}}', { time: formatTime(next) });
  return t('tasks.scheduleProgress.none', 'No next run');
}

function tabStatusOptions(type: TaskType): TaskStatus[] {
  return type === 'scheduled_run' ? SCHEDULED_STATUS_OPTIONS : BATCH_STATUS_OPTIONS;
}

function sandboxSemanticStatus(status?: TaskSandboxStatus | null): SemanticStatus {
  switch (status) {
    case 'running':
      return 'success';
    case 'starting':
    case 'pending':
      return 'running';
    case 'releasing':
      return 'warning';
    case 'lost':
    case 'failed_to_start':
      return 'danger';
    case 'released':
    default:
      return 'neutral';
  }
}

function taskName(task: Task, t: TFunction): string {
  const payload = task.payload && typeof task.payload === 'object'
    ? (task.payload as Record<string, unknown>)
    : {};
  const configuredName = typeof payload.name === 'string' ? payload.name.trim() : '';
  const isLegacyDefault = configuredName === 'Scheduled run' || configuredName === 'Batch execution';
  if (configuredName && !isLegacyDefault) return configuredName;
  return task.task_type === 'scheduled_run'
    ? t('tasks.type.scheduled_run', 'Scheduled run')
    : t('tasks.type.batch_exec', 'Batch run');
}

function workflowOptions(
  workflows: Array<{ wf_id: string; workflow_name?: string | null; description?: string | null }>,
): SearchSelectOption[] {
  return workflows.map((wf) => ({
    value: wf.wf_id,
    label: wf.workflow_name || wf.wf_id,
    meta: wf.wf_id,
    description: wf.description ?? '',
    keywords: [wf.workflow_name ?? '', wf.wf_id, wf.description ?? ''],
  }));
}

function duration(task: Task): string {
  if (!task.started_at) return '—';
  const end = task.finished_at ? Date.parse(task.finished_at) : Date.now();
  const start = Date.parse(task.started_at);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '—';
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return `${minutes}m ${rest}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function BatchTaskCreatePanel({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (taskId: string) => void;
}) {
  const { t } = useTranslation();
  const workflowsQuery = useWorkspaceList(200, 0);
  const workflows = useMemo(() => workflowsQuery.data?.items ?? [], [workflowsQuery.data?.items]);
  const workflowSelectOptions = useMemo(() => workflowOptions(workflows), [workflows]);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('');
  const effectiveWorkflowId = selectedWorkflowId || workflows[0]?.wf_id || '';

  const selectedWorkflow = workflows.find((wf) => wf.wf_id === effectiveWorkflowId);
  const versionSelection = useTaskWorkflowVersion(effectiveWorkflowId);
  const snapshotQuery = versionSelection.snapshot;
  const workflowSnapshot = snapshotQuery.data?.workflow ?? null;

  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden border border-edge-structural bg-surface-work" data-testid="task-batch-create-panel">
      <div className="shrink-0 border-b bg-surface-sunken/70 px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-base font-semibold">
              {t('tasks.new.batchTitle', 'Batch execution setup')}
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {t(
                'tasks.new.batchDesc',
                'Configure a workflow, input file, column mapping, and output columns before starting a batch task.',
              )}
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={onCancel}>
            {t('common.cancel', 'Cancel')}
          </Button>
        </div>
      </div>

      <div className="page-scroll-region grid flex-1 content-start gap-4 p-4 lg:grid-cols-[320px_minmax(0,1fr)]" data-role="task-create-scroll-region">
        <aside className="space-y-3">
          <div className="rounded-lg border bg-background p-3">
            <label className="text-sm font-medium" htmlFor="task-batch-workflow">
              {t('tasks.new.workflow', 'Workflow')}
            </label>
            <Suspense fallback={<DeferredControlFallback className="mt-2 h-10 w-full" />}>
              <SearchSelect
                value={effectiveWorkflowId}
                options={workflowSelectOptions}
                onValueChange={setSelectedWorkflowId}
                placeholder={t('tasks.new.selectWorkflow', 'Select a workflow to configure the batch task.')}
                searchPlaceholder={t('tasks.new.searchWorkflow', 'Search workflow name, ID, or description')}
                emptyText={t('tasks.new.noWorkflowMatches', 'No workflows match your search.')}
                disabled={workflowsQuery.isLoading || workflows.length === 0}
                className="mt-2"
                triggerClassName="w-full"
              />
            </Suspense>
            <TaskWorkflowVersion selection={versionSelection} batch />
            {workflowsQuery.isLoading ? (
              <p className="mt-2 text-xs text-muted-foreground">
                {t('workspace_loading', 'Loading workflows...')}
              </p>
            ) : workflows.length === 0 ? (
              <p className="mt-2 text-xs text-muted-foreground">
                {t('tasks.new.noWorkflows', 'Create a workflow before starting a batch task.')}
              </p>
            ) : selectedWorkflow ? (
              <div className="mt-3 space-y-1 rounded-md bg-surface-sunken p-2 text-xs text-muted-foreground">
                <div className="truncate font-medium text-foreground">
                  {selectedWorkflow.workflow_name}
                </div>
                <div className="truncate font-mono">{selectedWorkflow.wf_id}</div>
                {selectedWorkflow.description && (
                  <div className="line-clamp-3">{selectedWorkflow.description}</div>
                )}
              </div>
            ) : null}
          </div>

          <div className="rounded-lg border bg-background p-3 text-xs text-muted-foreground">
            <div className="font-medium text-foreground">
              {t('tasks.new.batchFlowTitle', 'What happens next')}
            </div>
            <ol className="mt-2 space-y-1.5">
              <li>{t('tasks.new.batchFlow1', 'Choose a workflow and load a tabular input file.')}</li>
              <li>{t('tasks.new.batchFlow2', 'Map input columns to the workflow StartNode fields.')}</li>
              <li>{t('tasks.new.batchFlow3', 'Start the task and open the live task detail page.')}</li>
            </ol>
          </div>
        </aside>

        <div className="min-w-0 flex-1">
          {(snapshotQuery.isLoading || versionSelection.loading) && effectiveWorkflowId ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
              {t('tasks.new.loadingWorkflow', 'Loading workflow...')}
            </div>
          ) : snapshotQuery.isError || versionSelection.error ? (
            <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
              {t('tasks.new.workflowLoadError', 'Failed to load workflow.')}
            </div>
          ) : effectiveWorkflowId && workflowSnapshot ? (
            <Suspense fallback={<DeferredControlFallback className="h-72 w-full" />}>
              <BatchTab
                wfId={effectiveWorkflowId}
                workflow={workflowSnapshot}
                versionTarget={versionSelection.frozenTarget}
                showTaskList={false}
                onSubmitted={onCreated}
              />
            </Suspense>
          ) : (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
              {t('tasks.new.selectWorkflow', 'Select a workflow to configure the batch task.')}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

export function TasksListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const formatTime = useFormatDateTime();
  const qc = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const resourceScope: ResourceListScope = searchParams.get('scope') === 'shared'
    ? 'shared'
    : 'owned';
  const activeType: TaskType = searchParams.get('type') === 'scheduled_run'
    ? 'scheduled_run'
    : 'batch_exec';
  const allowedStatuses = activeType === 'scheduled_run' ? SCHEDULED_STATUS_OPTIONS : BATCH_STATUS_OPTIONS;
  const statusFilter = (searchParams.get('status') ?? '')
    .split(',')
    .filter((value): value is TaskStatus => allowedStatuses.includes(value as TaskStatus));
  const queryText = searchParams.get('q') ?? '';
  const rawOffset = Number.parseInt(searchParams.get('offset') ?? '0', 10);
  const offset = Number.isFinite(rawOffset) && rawOffset > 0 ? rawOffset : 0;
  const [cancelTarget, setCancelTarget] = useState<Task | null>(null);
  const [shareTarget, setShareTarget] = useState<Task | null>(null);
  const [createMode, setCreateMode] = useState<TaskType | null>(null);

  const updateListParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(updates)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setSearchParams(next, { replace: true });
  };
  const setOffset = (next: number) => updateListParams({ offset: next > 0 ? String(next) : null });
  const setQueryText = (next: string) => updateListParams({ q: next || null, offset: null });
  const setStatusFilter = (
    next: TaskStatus[] | ((current: TaskStatus[]) => TaskStatus[]),
  ) => {
    const value = typeof next === 'function' ? next(statusFilter) : next;
    updateListParams({ status: value.length ? value.join(',') : null, offset: null });
  };
  const setActiveType = (next: TaskType) => updateListParams({
    type: next === 'batch_exec' ? null : next,
    status: null,
    offset: null,
  });

  const listQuery = useQuery({
    queryKey: ['tasks', { activeType, statusFilter, queryText, offset }],
    queryFn: () =>
      listTasks({
        status: statusFilter.length ? statusFilter : undefined,
        task_type: [activeType],
        q: queryText || undefined,
        limit: PAGE_SIZE,
        offset,
    }),
    placeholderData: (previous) => previous,
    refetchInterval: (query) => {
      const data = query.state.data;
      const hasActiveTask = data?.items?.some((task) =>
        ACTIVE_TASK_STATUSES.includes(task.status),
      );
      return hasActiveTask ? TASK_LIST_REFETCH_ACTIVE_MS : TASK_LIST_REFETCH_IDLE_MS;
    },
    refetchOnWindowFocus: false,
    enabled: resourceScope === 'owned',
  });

  const summaryQuery = useQuery({
    queryKey: ['tasks', 'summary', activeType],
    queryFn: () => getTaskSummary({ task_type: [activeType] }),
    refetchInterval: (query) => {
      const data = query.state.data;
      return (data?.active ?? 0) > 0 ? TASK_LIST_REFETCH_ACTIVE_MS : TASK_LIST_REFETCH_IDLE_MS;
    },
    refetchOnWindowFocus: false,
    enabled: resourceScope === 'owned',
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelTask(id, 'soft'),
    onSuccess: () => {
      setCancelTarget(null);
      qc.invalidateQueries({ queryKey: ['tasks'] });
      toast.success(t('tasks.cancel_requested', 'Cancel requested'));
    },
    onError: (e) => {
      toast.error(
        `${t('tasks.cancel_failed', 'Cancel failed')}: ${
          e instanceof Error ? e.message : String(e)
        }`,
      );
    },
  });

  const resumeMutation = useMutation({
    mutationFn: (id: string) => resumeTask(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] });
      toast.success(t('taskDetail.resumeRequested', 'Resume requested'));
    },
    onError: (e) => {
      toast.error(
        `${t('taskDetail.resumeFailed', 'Resume failed')}: ${
          e instanceof Error ? e.message : String(e)
        }`,
      );
    },
  });

  const items = useMemo(
    () =>
      [...(listQuery.data?.items ?? [])].sort((a, b) => {
        const rank = ACTIVE_RANK[a.status] - ACTIVE_RANK[b.status];
        if (rank !== 0) return rank;
        return (b.submitted_at ?? '').localeCompare(a.submitted_at ?? '');
      }),
    [listQuery.data?.items],
  );
  const total = listQuery.data?.total ?? 0;
  const hasNext = offset + PAGE_SIZE < total;
  const hasPrev = offset > 0;
  const summary = summaryQuery.data;

  const toggleStatus = (status: TaskStatus) => {
    setStatusFilter((prev) =>
      prev.includes(status) ? prev.filter((x) => x !== status) : [...prev, status],
    );
  };
  const selectTaskType = (type: TaskType) => {
    setActiveType(type);
  };
  const statusOptions = tabStatusOptions(activeType);
  const setResourceScope = (value: ResourceListScope) => updateListParams({
    scope: value === 'shared' ? 'shared' : null,
    type: null,
    status: null,
    offset: null,
  });

  if (resourceScope === 'shared') {
    return (
      <ManagementPageShell
        resourceKind="task"
        className="gap-5"
        title={t('tasks.title', 'Task')}
        description={t('tasks.subtitle', 'Batch and scheduled workflow runs')}
      >
        <ResourceScopeSwitch value={resourceScope} onValueChange={setResourceScope} />
        <div className="relative min-w-0 sm:max-w-md">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input
            value={queryText}
            onChange={(event) => setQueryText(event.target.value)}
            placeholder={t('tasks.searchShared', 'Search shared tasks')}
            className="pl-9"
          />
        </div>
        <SharedResourceList resourceType="task" search={queryText} />
      </ManagementPageShell>
    );
  }

  return (
    <>
      <ManagementPageShell
        resourceKind="task"
        className="gap-5"
        title={<span className="flex items-center gap-3">
          {t('tasks.title', 'Task')}
          {(summary?.active ?? 0) > 0 && (
            <StatusBadge status="running" data-testid="tasks-running-badge">
              {t('tasks.runningCount', '{{count}} running', { count: summary?.active ?? 0 })}
            </StatusBadge>
          )}
        </span>}
        description={t('tasks.subtitle', 'Batch and scheduled workflow runs')}
        actions={<>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void qc.invalidateQueries({ queryKey: ['tasks'] });
              }}
              disabled={listQuery.isFetching}
            >
              <RefreshCw className="h-4 w-4" />
              {t('tasks.action.refresh', 'Refresh')}
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm" data-testid="tasks-new-task-trigger">
                  {t('tasks.action.newTask', 'New Task')}
                  <ChevronDown className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <DropdownMenuItem
                  onClick={() => setCreateMode('batch_exec')}
                  data-testid="tasks-new-batch"
                >
                  <Download className="h-4 w-4" />
                  {t('tasks.new.batch', 'Batch execution')}
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => setCreateMode('scheduled_run')}
                  data-testid="tasks-new-scheduled"
                >
                  <CalendarClock className="h-4 w-4" />
                  {t('tasks.new.scheduled', 'Scheduled run')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </>}
      >

        <ResourceScopeSwitch value={resourceScope} onValueChange={setResourceScope} />

        {createMode === 'batch_exec' && (
          <BatchTaskCreatePanel
            onCancel={() => setCreateMode(null)}
            onCreated={(taskId) => {
              setCreateMode(null);
              void qc.invalidateQueries({ queryKey: ['tasks'] });
              navigate(`/tasks/${taskId}`);
            }}
          />
        )}

        {createMode === 'scheduled_run' && (
          <ScheduledRunCreatePanel
            onCancel={() => setCreateMode(null)}
            onCreated={(taskId) => {
              setCreateMode(null);
              void qc.invalidateQueries({ queryKey: ['tasks'] });
              navigate(`/tasks/${taskId}`);
            }}
          />
        )}

        {createMode === null && (
          <>
        <div className="border-b">
          <Tabs value={activeType} onValueChange={(value) => selectTaskType(value as TaskType)}>
            <TabsList variant="underline" className="h-10">
              <TabsTrigger
                value="batch_exec"
                className="h-10 px-4"
              >
                {t('tasks.type.batch_exec', 'Batch run')}
              </TabsTrigger>
              <TabsTrigger
                value="scheduled_run"
                className="h-10 px-4"
              >
                {t('tasks.type.scheduled_run', 'Scheduled run')}
              </TabsTrigger>
            </TabsList>
            <TabsContent forceMount value="batch_exec" className="hidden" />
            <TabsContent forceMount value="scheduled_run" className="hidden" />
          </Tabs>
        </div>

        <OperationalSummary
          label={t('tasks.summary.label', 'Task status summary')}
          items={activeType === 'scheduled_run'
            ? [
                { label: t('tasks.summary.enabled', 'Enabled'), value: summary?.enabled ?? 0, tone: 'success', hint: t('tasks.summaryHint.enabled', 'Schedules that will trigger future runs.') },
                { label: t('tasks.summary.paused', 'Paused'), value: summary?.paused ?? 0, tone: 'neutral', hint: t('tasks.summaryHint.paused', 'Schedules stopped by the user.') },
                { label: t('tasks.summary.running', 'Running'), value: (summary?.running ?? 0) + (summary?.resuming ?? 0), tone: 'info', hint: t('tasks.summaryHint.scheduledRunning', 'Scheduled runs currently executing.') },
                { label: t('tasks.summary.failed', 'Failed'), value: summary?.failed ?? 0, tone: 'danger', hint: t('tasks.summaryHint.scheduledFailed', 'Schedules whose latest state needs attention.') },
              ]
            : [
                { label: t('tasks.summary.running', 'Running'), value: (summary?.running ?? 0) + (summary?.resuming ?? 0), tone: 'info', hint: t('tasks.summaryHint.batchRunning', 'Batch jobs actively processing rows.') },
                { label: t('tasks.summary.queued', 'Queued'), value: summary?.queued ?? 0, tone: 'neutral', hint: t('tasks.summaryHint.queued', 'Batch jobs waiting for worker capacity.') },
                { label: t('tasks.summary.failed', 'Failed'), value: summary?.failed ?? 0, tone: 'danger', hint: t('tasks.summaryHint.batchFailed', 'Batch jobs that stopped with errors.') },
                { label: t('tasks.summary.finished', 'Finished'), value: summary?.finished ?? 0, tone: 'success', hint: t('tasks.summaryHint.finished', 'Batch jobs completed successfully.') },
              ]}
        />

        <ManagementToolbar className="flex-col items-stretch rounded-lg border-x border-edge-structural bg-surface-work">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <form
              className="relative w-full lg:max-w-sm"
              onSubmit={(event) => {
                event.preventDefault();
                const value = new FormData(event.currentTarget).get('q');
                setQueryText(typeof value === 'string' ? value.trim() : '');
              }}
            >
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                key={queryText}
                name="q"
                defaultValue={queryText}
                placeholder={t('tasks.searchPlaceholder', 'Search task or workflow ID')}
                className="pl-9"
              />
            </form>
            <div className="flex flex-wrap items-center gap-2">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    aria-label={t('tasks.filters.status', 'Filter by status')}
                  >
                    <ListFilter className="size-4" aria-hidden="true" />
                    {t('tasks.filters.statusLabel', 'Status')}
                    {statusFilter.length > 0 ? (
                      <span className="rounded-full bg-focus/10 px-1.5 text-xs font-semibold tabular-nums text-focus">
                        {statusFilter.length}
                      </span>
                    ) : null}
                    <ChevronDown className="size-3.5 text-muted-foreground" aria-hidden="true" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-56">
                  <DropdownMenuLabel>{t('tasks.filters.statusLabel', 'Status')}</DropdownMenuLabel>
                  {statusOptions.map((status) => (
                    <DropdownMenuCheckboxItem
                      key={status}
                      checked={statusFilter.includes(status)}
                      onSelect={(event) => event.preventDefault()}
                      onCheckedChange={() => toggleStatus(status)}
                    >
                      {t(`tasks.status.${status}`, status)}
                    </DropdownMenuCheckboxItem>
                  ))}
                  {statusFilter.length > 0 ? (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem onSelect={() => setStatusFilter([])}>
                        {t('tasks.filters.clear', 'Clear status filters')}
                      </DropdownMenuItem>
                    </>
                  ) : null}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>
          {statusFilter.length > 0 ? (
            <div className="mt-3 flex flex-wrap gap-1.5" aria-label={t('tasks.filters.selected', 'Selected status filters')}>
              {statusFilter.map((status) => (
                <button
                  key={status}
                  type="button"
                  onClick={() => toggleStatus(status)}
                  className="inline-flex h-7 items-center gap-1 rounded-full border border-focus/25 bg-focus/[0.07] px-2.5 text-xs font-medium text-focus hover:bg-focus/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                  aria-label={t('tasks.filters.removeStatus', 'Remove {{status}} filter', { status: t(`tasks.status.${status}`, status) })}
                >
                  {t(`tasks.status.${status}`, status)}
                  <X className="size-3" aria-hidden="true" />
                </button>
              ))}
            </div>
          ) : null}
        </ManagementToolbar>

        {listQuery.isLoading ? (
          <AsyncState kind="loading" title={t('tasks.loading', 'Loading…')} />
        ) : listQuery.isError ? (
          <ActionableError
            title={t('tasks.load_error', 'Failed to load tasks. Try again later.')}
            description={t('tasks.load_error_hint', 'Check your connection, then reload the task list.')}
            actionLabel={t('retry', 'Retry')}
            onAction={() => void listQuery.refetch()}
            technicalDetails={listQuery.error instanceof Error ? listQuery.error.message : String(listQuery.error)}
            technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
          />
        ) : items.length === 0 ? (
          <CompactEmptyState
            title={t('tasks.empty', 'No tasks yet.')}
            description={t('tasks.emptyHint', 'Batch runs and scheduled workflow activity will appear here.')}
          />
        ) : (
          <div
            className="resource-list-container app-scrollbar min-h-0 flex-1 overflow-auto border border-edge-structural bg-surface-work"
            data-testid="tasks-table-scroll"
          >
            <table role="table" className="resource-table resource-table-tasks w-full text-sm">
              <thead className="sticky top-0 z-10 border-b bg-surface-sunken text-left text-xs font-medium text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.status', 'Status')}</th>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.sandbox', 'Sandbox')}</th>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.task', 'Task')}</th>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.type', 'Type')}</th>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.workflow', 'Workflow')}</th>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.progress', 'Progress')}</th>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.duration', 'Duration')}</th>
                  <th className="px-4 py-3 font-medium">{t('tasks.col.created', 'Created')}</th>
                  <th className="px-4 py-3 text-right font-medium">{t('tasks.col.actions', 'Actions')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((task) => {
                  const pct = Math.round((task.progress ?? 0) * 100);
                  const capabilities = new Set(task.access?.capabilities ?? []);
                  const displayStatus = visibleTaskStatus(task.status);
                  return (
                    <tr
                      key={task.id}
                      className="border-b transition-colors last:border-b-0 hover:bg-surface-hover focus-within:bg-surface-hover"
                    >
                      <td data-col="status" data-label={t('tasks.col.status', 'Status')} className="px-4 py-3">
                        <StatusBadge status={taskSemanticStatus(displayStatus)}>
                          {t(`tasks.status.${displayStatus}`, displayStatus)}
                        </StatusBadge>
                      </td>
                      <td data-col="sandbox" data-label={t('tasks.col.sandbox', 'Sandbox')} className="px-4 py-3">
                        <span className="inline-flex whitespace-nowrap items-center gap-2 text-xs text-muted-foreground">
                          <StatusDot status={sandboxSemanticStatus(task.sandbox_status)} />
                          {t(`tasks.sandbox.${task.sandbox_status ?? 'released'}`, task.sandbox_status ?? 'released')}
                        </span>
                      </td>
                      <td data-col="name" className="max-w-[220px] px-4 py-3">
                        <div className="flex min-w-0 items-start gap-2.5">
                          <ResourceIcon kind="task" size="sm" />
                          <div className="min-w-0 flex-1">
                            <Link
                              to={`/tasks/${task.id}`}
                              className="block truncate font-medium underline-offset-4 hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                              title={taskName(task, t)}
                            >
                              {taskName(task, t)}
                            </Link>
                            <div className="font-mono text-xs text-muted-foreground">
                              {task.id.slice(0, 8)}
                            </div>
                            <ResourceProvenanceLine provenance={task.provenance} className="mt-0.5 flex" />
                          </div>
                        </div>
                      </td>
                      <td data-col="type" data-label={t('tasks.col.type', 'Type')} className="px-4 py-3 text-muted-foreground">
                        {t(`tasks.type.${task.task_type}`, task.task_type)}
                      </td>
                      <td data-col="workflow" data-label={t('tasks.col.workflow', 'Workflow')} className="max-w-[150px] truncate px-4 py-3 font-mono text-xs" title={task.workflow_id ?? ''}>
                        {task.workflow_id ?? '—'}
                      </td>
                      <td data-col="progress" data-label={t('tasks.col.progress', 'Progress')} className="px-4 py-3">
                        {task.task_type === 'scheduled_run' ? (
                          <span className="text-xs text-muted-foreground">
                            {formatScheduleProgress(task, formatTime, t)}
                          </span>
                        ) : (
                          <ProgressState
                            className="w-32"
                            status={taskSemanticStatus(task.status)}
                            label={<span className="sr-only">{t('tasks.col.progress', 'Progress')}</span>}
                            progressLabel={t('tasks.col.progress', 'Progress')}
                            detail={`${pct}%`}
                            value={pct}
                          />
                        )}
                      </td>
                      <td data-col="duration" data-label={t('tasks.col.duration', 'Duration')} className="px-4 py-3 text-xs text-muted-foreground">
                        {duration(task)}
                      </td>
                      <td title={formatTime(task.submitted_at)} data-col="created" data-label={t('tasks.col.created', 'Created')} className="px-4 py-3 text-xs text-muted-foreground">
                        {formatTime(task.submitted_at)}
                      </td>
                      <td data-col="actions" className="px-4 py-3 text-right">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild onClick={(event) => event.stopPropagation()}>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              aria-label={t('tasks.action.openMenu', 'Open task actions')}
                            >
                              <MoreHorizontal className="h-4 w-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem onClick={() => navigate(`/tasks/${task.id}`)}>
                              {t('tasks.action.details', 'Details')}
                            </DropdownMenuItem>
                            {task.task_type === 'scheduled_run' && (
                              <>
                                <DropdownMenuSeparator />
                                {capabilities.has('execute') ? (
                                  <DropdownMenuItem
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      runScheduledNow(task.id)
                                        .then(() => {
                                          toast.success(t('tasks.scheduled.runNowQueued', 'Run now queued'));
                                          void qc.invalidateQueries({ queryKey: ['tasks'] });
                                        })
                                        .catch((e) => {
                                          toast.error(e instanceof Error ? e.message : String(e));
                                        });
                                    }}
                                  >
                                    <Play className="h-4 w-4" />
                                    {t('tasks.scheduled.runNow', 'Run now')}
                                  </DropdownMenuItem>
                                ) : null}
                                {((task.payload as Record<string, unknown> | null)?.schedule_enabled === false || task.status === 'paused') && capabilities.has('update') ? (
                                  <DropdownMenuItem
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      resumeScheduledRun(task.id)
                                        .then(() => {
                                          toast.success(t('tasks.scheduled.resumed', 'Schedule resumed'));
                                          void qc.invalidateQueries({ queryKey: ['tasks'] });
                                        })
                                        .catch((e) => toast.error(e instanceof Error ? e.message : String(e)));
                                    }}
                                  >
                                    {t('tasks.scheduled.resume', 'Resume schedule')}
                                  </DropdownMenuItem>
                                ) : capabilities.has('update') && !(task.payload as Record<string, unknown> | null)?.schedule_completed ? (
                                  <DropdownMenuItem
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      pauseScheduledRun(task.id)
                                        .then(() => {
                                          toast.success(t('tasks.scheduled.paused', 'Schedule paused'));
                                          void qc.invalidateQueries({ queryKey: ['tasks'] });
                                        })
                                        .catch((e) => toast.error(e instanceof Error ? e.message : String(e)));
                                    }}
                                  >
                                    {t('tasks.scheduled.pause', 'Pause schedule')}
                                  </DropdownMenuItem>
                                ) : null}
                              </>
                            )}
                            {task.results_uri && capabilities.has('export') && (
                              <DropdownMenuItem asChild>
                                <a href={`/api/v1/tasks/${task.id}/download`}>
                                  {t('taskDetail.downloadCsv', 'Download CSV')}
                                </a>
                              </DropdownMenuItem>
                            )}
                            {CANCELLABLE.includes(task.status) && capabilities.has('cancel') && (
                              <>
                                <DropdownMenuSeparator />
                                <DropdownMenuItem
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setCancelTarget(task);
                                  }}
                                  className="text-state-danger focus:text-state-danger"
                                >
                                  {t('tasks.action.cancel', 'Cancel')}
                                </DropdownMenuItem>
                              </>
                            )}
                            {RESUMABLE.includes(task.status) && capabilities.has('resume')
                              && (task.result as { can_resume?: boolean } | null)?.can_resume !== false
                              && !!(task.result as { artifact_uris?: { jsonl?: string } } | null)?.artifact_uris?.jsonl && (
                              <>
                                <DropdownMenuSeparator />
                                <DropdownMenuItem
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    resumeMutation.mutate(task.id);
                                  }}
                                  disabled={resumeMutation.isPending}
                                >
                                  {t('taskDetail.resume', 'Resume')}
                                </DropdownMenuItem>
                              </>
                            )}
                            {capabilities.has('manage_access') ? (
                              <>
                                <DropdownMenuSeparator />
                                <DropdownMenuItem onClick={() => setShareTarget(task)}>
                                  <Share2 className="h-4 w-4" />
                                  {t('tasks.action.share', 'Share task')}
                                </DropdownMenuItem>
                              </>
                            ) : null}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <footer className="flex shrink-0 items-center justify-between text-sm text-muted-foreground">
          <span>
            {t('tasks.pagination', '{{start}}-{{end}} of {{total}}', {
              start: total === 0 ? 0 : offset + 1,
              end: Math.min(offset + PAGE_SIZE, total),
              total,
            })}
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!hasPrev}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              {t('common.previous', 'Previous')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!hasNext}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              {t('common.next', 'Next')}
            </Button>
          </div>
        </footer>
          </>
        )}
      </ManagementPageShell>

      <Dialog open={!!cancelTarget} onOpenChange={(open) => !open && setCancelTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('tasks.cancelConfirmTitle', 'Cancel this task?')}</DialogTitle>
            <DialogDescription>
              {t(
                'tasks.cancelConfirmDesc',
                'The backend will stop scheduling new work, release the sandbox, and close the task when cleanup is complete.',
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCancelTarget(null)}
              disabled={cancelMutation.isPending}
            >
              {t('taskDetail.dismiss', 'Cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={() => cancelTarget && cancelMutation.mutate(cancelTarget.id)}
              disabled={cancelMutation.isPending}
            >
              {t('tasks.action.cancel', 'Cancel')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {shareTarget ? (
        <ResourceShareDialog
          open
          onOpenChange={(open) => !open && setShareTarget(null)}
          resourceKind="task"
          resourceId={shareTarget.id}
          resourceName={taskName(shareTarget, t)}
          effectiveRole={shareTarget.access?.effective_role}
          accessSource={shareTarget.access?.source}
        />
      ) : null}
    </>
  );
}
