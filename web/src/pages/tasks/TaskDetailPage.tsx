/**
 * `/tasks/:taskId` task detail page.
 *
 * Two data sources, one screen:
 *   * TanStack Query polls `GET /tasks/{id}` for the canonical row
 *     (status, progress, result summary, results_uri). Polling pauses
 *     when the task reaches a terminal state — there is nothing left to
 *     refresh, and a stale 5s tick wastes a request.
 *   * `useTaskStream` opens the SSE channel for the live event log
 *     (`progress`, `started`, `finished`, `cancelled`, `error`,
 *     and any custom worker emissions).
 *
 * Why two channels instead of "compute everything from SSE":
 *   * The polled GET is RLS-bound to the same tenant + reuses the
 *     auth middleware (the 401 dialog), and it stays consistent across
 *     reconnects.
 *   * SSE is a delta channel — the worker emits `progress` frames as
 *     batch rows finish, but the canonical `progress` value lives in
 *     `tasks.progress` and is what the list page shows. Reading both
 *     and trusting the polled value avoids drift if the user opens
 *     this page after a stream drop.
 *
 * Cancel UX: one user-facing Cancel action safely stops the batch and preserves
 * partial artifacts. Internal cleanup/escalation details are not exposed.
 *
 * Download UX:
 *   * Result-bearing rows expose CSV, JSONL, and on-demand Excel downloads.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, Link, useSearchParams } from "react-router";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { ChevronDown, Download, FolderOpen, ListChecks, Share2 } from "lucide-react";

import { EntityDetailShell } from "@/components/layout/entity-detail-shell";
import {
  IncrementalLogLoader,
  LogHistoryControls,
} from "@/components/logs/log-history-controls";
import { resolveLogRange, type LogRangeValue, type LogSortOrder } from "@/lib/log-history";
import { SectionBlock } from "@/components/layout/section-block";
import { OperationalSummary } from "@/components/layout/operational-summary";
import { DetailSummary } from "@/components/layout/detail-summary";
import { ResourceShareDialog } from "@/components/modals/ResourceShareDialog";
import { ResourceProvenanceLine } from "@/components/resources/ResourceProvenanceLine";
import { ActionableError } from "@/components/presentation/ActionableError";
import { CodeSnippet } from "@/components/presentation/CodeSnippet";
import { CopyButton } from "@/components/ui/copy-button";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  ProgressState,
  StatusBadge,
  type SemanticStatus,
} from "@/components/ui/status";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { WorkflowVersionLink } from "@/components/resources/WorkflowVersionLink";
import { batchWorkflowVersion } from "@/lib/workflow/version-link";
import { EvaluationTab } from "./EvaluationTab";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  cancelTask,
  cancelScheduledExecution,
  getTask,
  getTaskEvents,
  getScheduledRun,
  listScheduledRunExecutions,
  pauseScheduledRun,
  resumeTask,
  resumeScheduledRun,
  runScheduledNow,
  type ScheduledRunExecution,
  type TaskEventPayload,
  type TaskEventType,
  type Task,
  type TaskStatus,
} from "@/lib/api/tasks";
import { useTaskStream, type TaskEventFrame } from "@/lib/api/sse/run-task-stream";
import { useFormatDateTime } from "@/lib/timezone";
import { ExecutionHistory } from "@/components/logs/execution-history";
import { describeCronExpression, scheduleLocale } from "@/lib/cron-description";

const POLL_INTERVAL_MS = 5_000;
const ACTIVE_STATUSES: TaskStatus[] = ["queued", "running", "resuming", "cancelling"];
const CANCELLABLE: TaskStatus[] = ["queued", "running", "resuming"];
const RESUMABLE: TaskStatus[] = ["cancelled", "failed", "interrupted", "finished_with_errors"];
const EVENT_TYPE_OPTIONS: TaskEventType[] = ["state", "progress", "log", "result", "terminal"];
const LEVEL_OPTIONS = ["all", "info", "warning", "error", "debug"] as const;

function taskStatusTone(s: TaskStatus): SemanticStatus {
  switch (s) {
    case "queued":
      return "neutral";
    case "running":
    case "resuming":
      return "running";
    case "finished":
      return "success";
    case "finished_with_errors":
    case "interrupted":
    case "cancelling":
      return "warning";
    case "failed":
      return "danger";
    case "cancelled":
    case "paused":
      return "neutral";
    case "enabled":
      return "success";
  }
}

/** Keep cleanup mechanics out of the user-facing task vocabulary. */
function visibleTaskStatus(status: TaskStatus): TaskStatus {
  return status === "interrupted"
    ? "cancelled"
    : status;
}

function taskDisplayName(task: Task, fallback: string): string {
  const payload = task.payload && typeof task.payload === "object"
    ? task.payload as Record<string, unknown>
    : {};
  return (
    (typeof payload.name === "string" && payload.name.trim()) || fallback
  );
}

function executionStatusTone(
  s: ScheduledRunExecution["status"],
): SemanticStatus {
  switch (s) {
    case "queued":
      return "neutral";
    case "running":
    case "cancelling":
      return "running";
    case "succeeded":
      return "success";
    case "failed":
      return "danger";
    case "cancelled":
    case "skipped":
      return "neutral";
  }
}

function eventLevelTone(level: string): SemanticStatus {
  if (level === "error") return "danger";
  if (level === "warning") return "warning";
  if (level === "debug") return "neutral";
  return "info";
}

/** Narrow the unknown `result` blob to the batch-run summary shape. */
interface BatchSummary {
  rows_total?: number;
  rows_ok?: number;
  rows_failed?: number;
}

interface BatchSetup {
  rows: number;
  mappedFields: number;
  concurrency: number;
  outputPath: string | null;
}

function asBatchSummary(r: unknown): BatchSummary | null {
  if (!r || typeof r !== "object") return null;
  const o = r as Record<string, unknown>;
  const out: BatchSummary = {};
  if (typeof o.rows_total === "number") out.rows_total = o.rows_total;
  if (typeof o.rows_ok === "number") out.rows_ok = o.rows_ok;
  if (typeof o.rows_failed === "number") out.rows_failed = o.rows_failed;
  return Object.keys(out).length > 0 ? out : null;
}

function asBatchSetup(payload: Task["payload"]): BatchSetup | null {
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  const source = record.data_source && typeof record.data_source === "object"
    ? record.data_source as Record<string, unknown>
    : null;
  const mapping = record.column_mapping && typeof record.column_mapping === "object"
    ? record.column_mapping as Record<string, unknown>
    : null;
  if (!source && !mapping) return null;
  const output = record.output && typeof record.output === "object"
    ? record.output as Record<string, unknown>
    : null;
  return {
    rows: Array.isArray(source?.rows) ? source.rows.length : 0,
    mappedFields: mapping ? Object.keys(mapping).length : 0,
    concurrency: typeof record.concurrency === "number" ? record.concurrency : 1,
    outputPath: typeof output?.path === "string" ? output.path : null,
  };
}

function isTaskStatus(value: unknown): value is TaskStatus {
  return typeof value === "string" && [
    "queued",
    "running",
    "resuming",
    "finished",
    "finished_with_errors",
    "failed",
    "interrupted",
    "cancelling",
    "cancelled",
    "enabled",
    "paused",
  ].includes(value);
}

function eventTaskPatch(task: Task, frame: TaskEventFrame): Partial<Task> | null {
  const payload = frame.payload;
  const patch: Partial<Task> = {};

  if (isTaskStatus(payload.task_status)) {
    patch.status = payload.task_status;
  }
  if (payload.sandbox_status && typeof payload.sandbox_status === "string") {
    patch.sandbox_status = payload.sandbox_status as Task["sandbox_status"];
  }
  if (typeof payload.progress?.percent === "number") {
    patch.progress = Math.max(0, Math.min(1, payload.progress.percent));
  }
  if (payload.error?.message) {
    patch.error = payload.error.message;
  }

  const data = payload.data && typeof payload.data === "object"
    ? payload.data as Record<string, unknown>
    : {};
  if (typeof data.results_uri === "string") {
    patch.results_uri = data.results_uri;
  }
  if (data.summary && typeof data.summary === "object") {
    patch.result = data.summary;
  }

  if (payload._event_ts) {
    if (patch.status === "running" && !task.started_at) {
      patch.started_at = payload._event_ts;
    }
    if (frame.event_type === "terminal" && !task.finished_at) {
      patch.finished_at = payload._event_ts;
    }
  }

  return Object.keys(patch).length > 0 ? patch : null;
}

/**
 * Pull `{done, total}` from the most recent `progress` SSE frame. The batch
 * worker emits `progress` frames carrying per-row counts as rows finish — we
 * surface them as a plain "X of Y rows done" line so a non-technical user
 * reads their batch at a glance instead of decoding a bare percentage.
 * Returns null until a usable progress frame arrives.
 */
function latestRowCounts(
  events: TaskEventFrame[],
): { done: number; total: number } | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const f = events[i];
    if (f.event_type !== "progress") continue;
    const p = f.payload.progress;
    if (p && typeof p.done === "number" && typeof p.total === "number") {
      return { done: p.done, total: p.total };
    }
  }
  return null;
}

function humanTaskError(raw: string, fallback: string): string {
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object') return raw.trim() || fallback;
    const record = parsed as Record<string, unknown>;
    const candidate = record.message ?? record.error ?? record.detail ?? record.reason;
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
    if (candidate && typeof candidate === 'object') {
      const nested = candidate as Record<string, unknown>;
      const nestedMessage = nested.message ?? nested.detail ?? nested.reason;
      if (typeof nestedMessage === 'string' && nestedMessage.trim()) return nestedMessage.trim();
    }
    return fallback;
  } catch {
    return raw.trim() || fallback;
  }
}

/** Render one event frame as a compact log row. */
function EventRow({
  frame,
  formatTime,
  nowLabel,
}: {
  frame: TaskEventFrame;
  formatTime: (value?: string | null) => string;
  nowLabel: string;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const payload = frame.payload;
  const level = payload.level ?? "info";
  const message = payload.message || payload.error?.message || payload.action ||
    t(`taskDetail.eventType.${frame.event_type}`, frame.event_type);
  const scope = payload.scope
    ? [payload.scope.type, payload.scope.id].filter(Boolean).join(":")
    : "";
  const payloadStr = useMemo(() => {
    if (!expanded) return "";
    try {
      return JSON.stringify(
        {
          progress: payload.progress,
          data: payload.data,
          error: payload.error,
        },
        null,
        2,
      );
    } catch {
      return "";
    }
  }, [expanded, payload]);

  return (
    <li className="border-b border-edge-subtle py-2.5 last:border-b-0">
      <button
        type="button"
        className="grid w-full grid-cols-[auto_minmax(0,1fr)] items-start gap-3 rounded-md px-1 py-1 text-left hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <StatusBadge className="w-fit" status={eventLevelTone(level)}>
          {t(`taskDetail.level.${level}`, level)}
        </StatusBadge>
        <span className="min-w-0">
          <span className="block break-words text-xs font-medium leading-5 [overflow-wrap:anywhere]">{message}</span>
          <span className="mt-0.5 block truncate text-xs text-content-tertiary">
            {(payload as TaskEventPayload & { _event_ts?: string })._event_ts
              ? formatTime((payload as TaskEventPayload & { _event_ts?: string })._event_ts)
              : nowLabel}
            {` · ${t(`taskDetail.eventType.${frame.event_type}`, frame.event_type)}`}
            {scope ? ` · ${scope}` : ""}
          </span>
        </span>
      </button>
      {expanded && payloadStr !== "{}" && (
        <CodeSnippet className="mt-2" code={payloadStr} language="json" />
      )}
    </li>
  );
}

export function TaskDetailPage() {
  const { t, i18n } = useTranslation();
  // Render UTC timestamps in the user's chosen timezone (reactive).
  const formatTime = useFormatDateTime();
  const { taskId } = useParams<{ taskId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const qc = useQueryClient();
  const [shareOpen, setShareOpen] = useState(false);
  const [eventTypeFilter, setEventTypeFilter] = useState<TaskEventType | "all">("all");
  const [levelFilter, setLevelFilter] = useState<(typeof LEVEL_OPTIONS)[number]>("all");
  const [selectedExecutionId, setSelectedExecutionId] = useState<string | null>(null);
  const [logRange, setLogRange] = useState<LogRangeValue>({ range: "all", from: "", to: "" });
  const [logOrder, setLogOrder] = useState<LogSortOrder>("desc");
  const eventLogRegionRef = useRef<HTMLDivElement>(null);
  const activeTab = ["logs", "evaluation", "settings"].includes(searchParams.get("tab") ?? "") ? searchParams.get("tab")! : "overview";
  const logBounds = useMemo(
    () => resolveLogRange(logRange),
    [logRange],
  );

  const taskQuery = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => getTask(taskId!),
    enabled: !!taskId,
    // Stop polling once the row reaches a terminal state — nothing
    // left to refresh and we want to be a good network citizen.
    refetchInterval: (q) => {
      const data = q.state.data as Task | undefined;
      if (!data) return POLL_INTERVAL_MS;
      return ACTIVE_STATUSES.includes(data.status)
        ? POLL_INTERVAL_MS
        : false;
    },
    refetchOnWindowFocus: false,
  });

  const eventsQuery = useInfiniteQuery({
    queryKey: ["task", taskId, "events", eventTypeFilter, logRange, logOrder],
    queryFn: ({ pageParam }) => getTaskEvents(taskId!, {
      limit: 50,
      order: logOrder,
      event_type: eventTypeFilter === "all" ? undefined : [eventTypeFilter],
      ...logBounds,
      ...(pageParam == null
        ? {}
        : logOrder === "desc"
          ? { before_seq: pageParam }
          : { after_seq: pageParam }),
    }),
    initialPageParam: null as number | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    placeholderData: (previousData) => previousData,
    enabled: !!taskId && (taskQuery.data?.access?.capabilities.includes("inspect_runs") ?? false),
    refetchOnWindowFocus: false,
  });
  const latestEventSeq = eventsQuery.data?.pages[0]?.latest_seq ?? 0;
  const stream = useTaskStream(
    taskId,
    (taskQuery.data?.access?.capabilities.includes("inspect_runs") ?? false) && eventsQuery.isSuccess,
    latestEventSeq,
  );
  const scheduledQuery = useQuery({
    queryKey: ["task", taskId, "scheduled-run"],
    queryFn: () => getScheduledRun(taskId!),
    enabled: !!taskId && taskQuery.data?.task_type === "scheduled_run",
    refetchInterval: (q) => {
      const data = q.state.data;
      return data?.task.status === "running" ? POLL_INTERVAL_MS : false;
    },
    refetchOnWindowFocus: false,
  });
  const executionsQuery = useQuery({
    queryKey: ["task", taskId, "scheduled-run", "executions"],
    queryFn: () => listScheduledRunExecutions(taskId!, { limit: 50 }),
    enabled: !!taskId && taskQuery.data?.task_type === "scheduled_run",
    refetchInterval: (q) => {
      const data = q.state.data;
      return data?.items.some((x) => x.status === "running" || x.status === "queued")
        ? POLL_INTERVAL_MS
        : false;
    },
    refetchOnWindowFocus: false,
  });
  const executions = useMemo(() => executionsQuery.data?.items ?? [], [executionsQuery.data?.items]);
  const selectedExecution = useMemo(() => {
    if (!executions.length) return null;
    return executions.find((x) => x.id === selectedExecutionId) ?? executions[0];
  }, [executions, selectedExecutionId]);

  useEffect(() => {
    if (!taskId || stream.events.length === 0) return;
    const frame = stream.events[stream.events.length - 1];
    qc.setQueryData<Task>(["task", taskId], (old) => {
      if (!old) return old;
      const patch = eventTaskPatch(old, frame);
      return patch ? { ...old, ...patch } : old;
    });
    if (frame.event_type === "terminal") {
      void qc.invalidateQueries({ queryKey: ["tasks"] });
      void qc.invalidateQueries({ queryKey: ["task", taskId] });
      // The task prefix already includes schedule, executions and event pages.
    }
  }, [qc, stream.events, taskId, taskQuery.data?.task_type]);

  const allEvents = useMemo(() => {
    const byId = new Map<number, TaskEventFrame>();
    for (const event of eventsQuery.data?.pages.flatMap((page) => page.items) ?? []) {
      if (byId.has(event.id)) continue;
      byId.set(event.id, {
        id: event.id,
        event_type: event.event_type,
        payload: event.ts && !event.payload._event_ts
          ? { ...event.payload, _event_ts: event.ts }
          : event.payload,
      });
    }
    for (const event of stream.events) byId.set(event.id, event);
    return [...byId.values()].sort((a, b) => logOrder === "desc" ? b.id - a.id : a.id - b.id);
  }, [eventsQuery.data?.pages, logOrder, stream.events]);

  const visibleEvents = useMemo(
    () =>
      allEvents.filter((event) => {
        if (eventTypeFilter !== "all" && event.event_type !== eventTypeFilter) return false;
        const eventTimestamp = event.payload._event_ts ? Date.parse(event.payload._event_ts) : Number.NaN;
        if (logBounds.from && Number.isFinite(eventTimestamp) && eventTimestamp < Date.parse(logBounds.from)) return false;
        if (logBounds.to && Number.isFinite(eventTimestamp) && eventTimestamp > Date.parse(logBounds.to)) return false;
        const level = event.payload.level ?? "info";
        if (levelFilter !== "all" && level !== levelFilter) return false;
        if (taskQuery.data?.task_type === "scheduled_run" && selectedExecution) {
          const data = event.payload.data;
          const scope = event.payload.scope;
          const matchesData =
            data && typeof data === "object" &&
            (data as Record<string, unknown>).execution_id === selectedExecution.id;
          const matchesScope =
            scope && typeof scope === "object" &&
            scope.id === selectedExecution.id;
          if (!matchesData && !matchesScope) return false;
        }
        return true;
      }),
    [allEvents, eventTypeFilter, levelFilter, logBounds.from, logBounds.to, selectedExecution, taskQuery.data?.task_type],
  );

  const { fetchNextPage: fetchNextEventPage, hasNextPage: hasNextEventPage, isFetchingNextPage: isFetchingNextEventPage } = eventsQuery;
  const loadMoreEvents = useCallback(() => {
    if (hasNextEventPage && !isFetchingNextEventPage) {
      void fetchNextEventPage();
    }
  }, [fetchNextEventPage, hasNextEventPage, isFetchingNextEventPage]);

  const cancelMutation = useMutation({
    mutationFn: () => cancelTask(taskId!, "soft"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(t("tasks.cancel_requested", "Cancel requested"));
    },
    onError: (e) => {
      toast.error(
        `${t("tasks.cancel_failed", "Cancel failed")}: ${
          e instanceof Error ? e.message : String(e)
        }`,
      );
    },
  });

  const resumeMutation = useMutation({
    mutationFn: () => resumeTask(taskId!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(t("taskDetail.resumeRequested", "Resume requested"));
    },
    onError: (e) => {
      toast.error(
        `${t("taskDetail.resumeFailed", "Resume failed")}: ${
          e instanceof Error ? e.message : String(e)
        }`,
      );
    },
  });

  const runNowMutation = useMutation({
    mutationFn: () => runScheduledNow(taskId!),
    onSuccess: (data) => {
      setSelectedExecutionId(data.execution.id);
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(t("tasks.scheduled.runNowQueued", "Run now queued"));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  const pauseScheduleMutation = useMutation({
    mutationFn: () => pauseScheduledRun(taskId!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(t("tasks.scheduled.paused", "Schedule paused"));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  const resumeScheduleMutation = useMutation({
    mutationFn: () => resumeScheduledRun(taskId!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(t("tasks.scheduled.resumed", "Schedule resumed"));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  const cancelExecutionMutation = useMutation({
    mutationFn: (executionId: string) => cancelScheduledExecution(taskId!, executionId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success(t("tasks.cancel_requested", "Cancel requested"));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  if (!taskId) {
    return (
      <div className="flex-1 p-6 text-sm text-muted-foreground">
        {t("taskDetail.invalidId", "Missing task id in URL")}
      </div>
    );
  }

  if (taskQuery.isLoading) {
    return (
      <div className="flex-1 p-6 text-sm text-muted-foreground">
        {t("tasks.loading", "Loading…")}
      </div>
    );
  }

  if (taskQuery.isError || !taskQuery.data) {
    return (
      <div className="flex-1 p-6">
        <ActionableError
          title={t(
            "taskDetail.loadError",
            "Failed to load this task. It may have been deleted or you may not have access.",
          )}
          description={t("taskDetail.loadErrorHint", "Return to the task list or try loading this task again.")}
          actionLabel={t("retry", "Retry")}
          onAction={() => void taskQuery.refetch()}
          technicalDetails={taskQuery.error instanceof Error ? taskQuery.error.message : String(taskQuery.error ?? "")}
          technicalDetailsLabel={t("technicalDetails", "Technical details")}
        />
        <div className="mt-4">
          <Link
            to="/tasks"
            className="text-sm text-primary underline-offset-4 hover:underline"
          >
            ← {t("taskDetail.backToList", "Back to tasks")}
          </Link>
        </div>
      </div>
    );
  }

  const task = taskQuery.data;
  const displayStatus = visibleTaskStatus(task.status);
  const capabilities = new Set(task.access?.capabilities ?? []);
  const pct = Math.round((task.progress ?? 0) * 100);
  const isScheduledRun = task.task_type === "scheduled_run";
  const isCancellable = !isScheduledRun && capabilities.has("cancel") && CANCELLABLE.includes(task.status);
  const isResumable = !isScheduledRun && capabilities.has("resume") && RESUMABLE.includes(task.status)
    && (task.result as { can_resume?: boolean } | null)?.can_resume !== false
    && !!(task.result as { artifact_uris?: { jsonl?: string } } | null)?.artifact_uris?.jsonl;
  const summary = ["finished", "finished_with_errors", "interrupted"].includes(task.status)
    ? asBatchSummary(task.result)
    : null;
  const configuredVersion = scheduledQuery.data?.schedule.workflow_selector?.version;
  const linkedVersion = isScheduledRun ? configuredVersion ?? selectedExecution?.version : batchWorkflowVersion(task);
  const linkedWorkflowId = isScheduledRun && !configuredVersion ? selectedExecution?.workflow_id : task.workflow_id;
  const batchSetup = isScheduledRun ? null : asBatchSetup(task.payload);
  // "X of Y rows done" — prefer the live SSE progress frame; once finished,
  // the summary card carries the authoritative totals so we pin done==total.
  const liveCounts = latestRowCounts(allEvents);
  const rowCounts = summary && task.status !== "interrupted"
    ? typeof summary.rows_total === "number"
      ? { done: summary.rows_total, total: summary.rows_total }
      : null
    : liveCounts;
  const canDownload = !isScheduledRun && capabilities.has("export") && !!task.results_uri;
  const downloadHref = `/api/v1/tasks/${taskId}/download`;
  const canViewResult = !ACTIVE_STATUSES.includes(task.status) && !!(task.result as { artifact_uris?: { jsonl?: string } } | null)?.artifact_uris?.jsonl;
  const selectTab = (tab: string) => {
    const next = new URLSearchParams(searchParams);
    if (tab === "logs" || tab === "evaluation" || tab === "settings") next.set("tab", tab);
    else next.delete("tab");
    setSearchParams(next, { replace: true });
  };

  return (
    <EntityDetailShell
      resourceKind="task"
      backTo="/tasks"
      backLabel={t("taskDetail.backToList", "Back to tasks")}
      title={isScheduledRun && scheduledQuery.data?.schedule.name || taskDisplayName(
        task,
        task.task_type === "scheduled_run"
          ? t("tasks.type.scheduled_run", "Scheduled run")
          : t("tasks.type.batch_exec", "Batch execution"),
      )}
      icon={ListChecks}
      status={
        <StatusBadge status={taskStatusTone(displayStatus)}>
          {t(`tasks.status.${displayStatus}`, displayStatus)}
        </StatusBadge>
      }
      metadata={(<>
        <span className="inline-flex min-w-0 items-center gap-1">
          <span className="font-mono" title={task.id}>{t("taskDetail.taskId", "Task ID")}: {task.id.slice(0, 8)}…</span>
          <CopyButton value={task.id} />
        </span>
        {task.workflow_id && <span className="inline-flex min-w-0 items-center gap-1">
          <span className="min-w-0 break-all font-mono">{t("tasks.col.workflow", "Workflow")}: {task.workflow_id}</span>
          <CopyButton className="shrink-0" value={task.workflow_id} />
        </span>}
        <WorkflowVersionLink workflowId={linkedWorkflowId} version={linkedVersion}
          inline kind={isScheduledRun ? configuredVersion ? "configured" : "execution" : "snapshot"} />
        <ResourceProvenanceLine provenance={task.provenance} />
      </>)}
      actions={
        <>
              {canDownload && (
                <>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="outline" size="sm">
                        <Download aria-hidden="true" />
                        {t("taskDetail.download", "Download")}
                        <ChevronDown aria-hidden="true" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem asChild>
                        <a href={`${downloadHref}?format=csv`}>CSV</a>
                      </DropdownMenuItem>
                      <DropdownMenuItem asChild>
                        <a href={`${downloadHref}?format=jsonl`}>JSONL</a>
                      </DropdownMenuItem>
                      <DropdownMenuItem asChild>
                        <a href={`${downloadHref}?format=xlsx`}>Excel (.xlsx)</a>
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>

                </>
              )}
              {!isScheduledRun && (canViewResult ? <Button variant="outline" size="sm" asChild>
                <Link to={`/tasks/${task.id}/results`}><FolderOpen aria-hidden="true" />{t("evaluation.viewResult", "View Result")}</Link>
              </Button> : <Button variant="outline" size="sm" disabled>{t("evaluation.viewResult", "View Result")}</Button>)}
              {isCancellable && (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => cancelMutation.mutate()}
                    disabled={cancelMutation.isPending}
                  >
                    {t("tasks.action.cancel", "Cancel")}
                  </Button>
                </>
              )}
              {isResumable && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => resumeMutation.mutate()}
                  disabled={resumeMutation.isPending}
                >
                  {t("taskDetail.resume", "Resume")}
                </Button>
              )}
              {isScheduledRun && (capabilities.has("execute") || capabilities.has("update") || capabilities.has("cancel")) && (
                <>
                  {capabilities.has("execute") ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => runNowMutation.mutate()}
                      disabled={runNowMutation.isPending || task.status === "running"}
                    >
                      {t("tasks.scheduled.runNow", "Run now")}
                    </Button>
                  ) : null}
                  {capabilities.has("update") ? task.status === "paused" ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => resumeScheduleMutation.mutate()}
                        disabled={resumeScheduleMutation.isPending}
                      >
                        {t("tasks.scheduled.resume", "Resume schedule")}
                      </Button>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => pauseScheduleMutation.mutate()}
                        disabled={pauseScheduleMutation.isPending}
                      >
                        {t("tasks.scheduled.pause", "Pause schedule")}
                      </Button>
                    ) : null}
                  {selectedExecution?.status === "running" && capabilities.has("cancel") && (
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => cancelExecutionMutation.mutate(selectedExecution.id)}
                      disabled={cancelExecutionMutation.isPending}
                    >
                      {t("tasks.action.cancel", "Cancel")}
                    </Button>
                  )}
                </>
              )}
              {capabilities.has("manage_access") ? (
                <Button variant="outline" size="sm" onClick={() => setShareOpen(true)}>
                  <Share2 aria-hidden="true" />
                  {t("tasks.action.share", "Share task")}
                </Button>
              ) : null}
        </>
      }
      className="max-w-5xl gap-0"
    >
      <Tabs
        value={isScheduledRun && activeTab === "evaluation" ? "overview" : activeTab}
        onValueChange={selectTab}
        className="shrink-0"
      >
        <TabsList
          variant="underline"
          className="chat-scrollbar flex h-auto w-full justify-start overflow-x-auto border-b border-edge-subtle"
          aria-label={t("taskDetail.tabsLabel", "Task detail sections")}
        >
          <TabsTrigger value="overview" className="shrink-0">
            {t("taskDetail.tab.overview", "Execution overview")}
          </TabsTrigger>
          <TabsTrigger value="logs" className="shrink-0">
            {t("taskDetail.tab.logs", "Execution logs")}
          </TabsTrigger>
          <TabsTrigger value="settings" className="shrink-0">{t("taskDetail.tab.settings", "Configuration")}</TabsTrigger>
          {!isScheduledRun && <TabsTrigger value="evaluation">{t("evaluation.title", "Evaluation")}</TabsTrigger>}
        </TabsList>

        <TabsContent value="overview" className="mt-0">

        {/* An idle schedule has no meaningful completion percentage. */}
        {(!isScheduledRun || task.status === "running") && <SectionBlock
          variant="plain"
          title={summary ? t("taskDetail.summary", "Summary") : t("tasks.col.progress", "Progress")}
          description={!summary && rowCounts
            ? t("taskDetail.rowsProgress", "{{done}} of {{total}} rows done", { done: rowCounts.done, total: rowCounts.total })
            : undefined}
          actions={!summary ? <span className="text-sm font-semibold tabular-nums text-content-secondary">{pct}%</span> : undefined}
        >
          {!summary && rowCounts ? <span className="sr-only" data-testid="row-progress">
            {t("taskDetail.rowsProgress", "{{done}} of {{total}} rows done", { done: rowCounts.done, total: rowCounts.total })}
          </span> : null}
          {summary ? (
            <dl className="grid grid-cols-3 gap-x-6 gap-y-2 text-sm">
              <div className="flex flex-col">
                <dt className="text-xs text-muted-foreground">
                  {t("taskDetail.rowsTotal", "Rows total")}
                </dt>
                <dd className="tabular-nums">{summary.rows_total ?? "—"}</dd>
              </div>
              <div className="flex flex-col">
                <dt className="text-xs text-muted-foreground">
                  {t("taskDetail.rowsOk", "Rows ok")}
                </dt>
                <dd className="tabular-nums text-state-success">
                  {summary.rows_ok ?? "—"}
                </dd>
              </div>
              <div className="flex flex-col">
                <dt className="text-xs text-muted-foreground">
                  {t("taskDetail.rowsFailed", "Rows failed")}
                </dt>
                <dd className="tabular-nums text-destructive">
                  {summary.rows_failed ?? "—"}
                </dd>
              </div>
            </dl>
          ) : <ProgressState
            status={taskStatusTone(task.status)}
            label={<span className="sr-only">{t("tasks.col.progress", "Progress")}</span>}
            progressLabel={t("tasks.col.progress", "Progress")}
            value={pct}
          />}
          <dl className="mt-4 grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-3">
            <div className="flex flex-col">
              <dt className="text-muted-foreground">
                {t("taskDetail.submittedAt", "Submitted")}
              </dt>
              <dd>{formatTime(task.submitted_at)}</dd>
            </div>
            <div className="flex flex-col">
              <dt className="text-muted-foreground">
                {t("taskDetail.startedAt", "Started")}
              </dt>
              <dd>{formatTime(task.started_at)}</dd>
            </div>
            <div className="flex flex-col">
              <dt className="text-muted-foreground">
                {t("taskDetail.finishedAt", "Finished")}
              </dt>
              <dd>{formatTime(task.finished_at)}</dd>
            </div>
          </dl>
        </SectionBlock>}

        {isScheduledRun && (
          <div className="contents">
            {scheduledQuery.data ? (
              <OperationalSummary
                label={t("tasks.scheduled.operationalSummary", "Schedule summary")}
                className="mt-5"
                items={[
                  {
                    label: t("tasks.scheduled.nextRun", "Next run"),
                    value: formatTime(scheduledQuery.data.schedule.next_run_at),
                    tone: task.status === "paused" ? "neutral" : "info",
                  },
                  {
                    label: t("taskDetail.lastRun", "Last run"),
                    value: formatTime(scheduledQuery.data.schedule.last_run_at),
                  },
                  {
                    label: t("tasks.scheduled.lastStatus", "Last status"),
                    value: scheduledQuery.data.schedule.last_status
                      ? t(`tasks.executionStatus.${scheduledQuery.data.schedule.last_status}`, scheduledQuery.data.schedule.last_status)
                      : "—",
                    tone: scheduledQuery.data.schedule.last_status === "succeeded" ? "success" : "neutral",
                  },
                ]}
              />
            ) : null}

          </div>
        )}

        {/* Error block (failed only) */}
        {task.status === "failed" && task.error && (
          <ActionableError
            title={t("taskDetail.error", "Task execution failed")}
            description={humanTaskError(
              task.error,
              t(
                "taskDetail.errorHint",
                "The task could not finish. Review the input and workflow configuration, then run it again.",
              ),
            )}
            technicalDetails={task.error}
            technicalDetailsLabel={t("technicalDetails", "Technical details")}
          />
        )}

        </TabsContent>

        <TabsContent value="settings" className="mt-0">
        {!isScheduledRun && !batchSetup && <p className="py-6 text-sm text-muted-foreground">{t("taskDetail.noConfiguration", "No saved input configuration is available for this task.")}</p>}
        {batchSetup && (
          <SectionBlock
            variant="plain"
            title={t("taskDetail.setup", "Task setup")}
            description={t(
              "taskDetail.setupDescription",
              "Input and processing settings captured when this batch was submitted.",
            )}
          >
            <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-xs text-content-tertiary">{t("taskDetail.inputRows", "Input rows")}</dt>
                <dd className="mt-1 font-medium tabular-nums">{batchSetup.rows}</dd>
              </div>
              <div>
                <dt className="text-xs text-content-tertiary">{t("taskDetail.mappedFields", "Mapped fields")}</dt>
                <dd className="mt-1 font-medium tabular-nums">{batchSetup.mappedFields}</dd>
              </div>
              <div>
                <dt className="text-xs text-content-tertiary">{t("taskDetail.parallelRows", "Parallel rows")}</dt>
                <dd className="mt-1 font-medium tabular-nums">{batchSetup.concurrency}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-xs text-content-tertiary">{t("taskDetail.output", "Output")}</dt>
                <dd className="mt-1 flex min-w-0 items-start gap-1 font-medium">
                  <span className="min-w-0 break-all">{batchSetup.outputPath ?? t("taskDetail.outputManaged", "Managed by Flowork")}</span>
                  {batchSetup.outputPath && <CopyButton className="shrink-0" value={batchSetup.outputPath} />}
                </dd>
              </div>
            </dl>
          </SectionBlock>
        )}

          {isScheduledRun && (
            <SectionBlock
              variant="plain"
              title={t("tasks.scheduled.configuration", "Schedule configuration")}
              description={t("tasks.scheduled.configurationDescription", "Timing and workflow settings used for each scheduled execution.")}
            >
              {scheduledQuery.isLoading ? (
                <div className="mt-3 text-sm text-muted-foreground">
                  {t("tasks.loading", "Loading…")}
                </div>
              ) : scheduledQuery.data ? (
                <div data-testid="schedule-configuration-details">
                  <DetailSummary
                    className="mt-1 max-w-3xl gap-x-10 gap-y-6"
                    items={[
                    {
                      label: t("tasks.scheduled.timing", "Timing"),
                      value: scheduledQuery.data.schedule.schedule_type === "interval"
                        ? t("tasks.scheduled.everySeconds", "Every {{count}} seconds", {
                            count: scheduledQuery.data.schedule.interval_seconds ?? 0,
                          })
                        : (
                          <span>
                            <span className="block">
                              {describeCronExpression(
                                scheduledQuery.data.schedule.cron_expr,
                                scheduleLocale(i18n.resolvedLanguage),
                              ).text}
                            </span>
                            <code className="mt-1 block font-mono text-xs text-muted-foreground" translate="no">
                              {scheduledQuery.data.schedule.cron_expr}
                            </code>
                          </span>
                        ),
                    },
                    {
                      label: t("tasks.scheduled.timezone", "Timezone"),
                      value: <span translate="no">{scheduledQuery.data.schedule.timezone}</span>,
                    },
                    {
                      label: t("tasks.scheduled.fixedInputs", "Fixed inputs"),
                      value: <span className="tabular-nums">{Object.keys(scheduledQuery.data.schedule.input_preset ?? {}).length}</span>,
                    },
                    ]}
                  />
                </div>
              ) : (
                <div className="mt-3 text-sm text-muted-foreground">
                  {t("taskDetail.loadError", "Failed to load this task. It may have been deleted or you may not have access.")}
                </div>
              )}
            </SectionBlock>
          )}
        </TabsContent>

        {/* Live event log */}
        <TabsContent value="logs" className="mt-0">
          {capabilities.has("inspect_runs") && <ExecutionHistory key={taskId} source="task" sourceId={taskId ?? ''} />}
          {isScheduledRun && (
            <SectionBlock
              variant="plain"
              collapsible
              defaultOpen={false}
              title={t("taskDetail.triggerHistory", "Schedule triggers")}
              description={t("tasks.scheduled.runHistoryDescription", "Select an execution to focus its timing, result, and event context.")}
              actions={executionsQuery.isFetching ? <span className="text-xs text-muted-foreground">{t("tasks.loading", "Loading…")}</span> : null}
            >
              {executionsQuery.isError && (
                <ActionableError
                  className="mb-3"
                  title={t("taskDetail.executionLoadError", "Could not load execution history")}
                  actionLabel={t("retry", "Retry")}
                  onAction={() => void executionsQuery.refetch()}
                />
              )}
              {executionsQuery.isPending ? (
                <div role="status" className="rounded-lg border border-edge-subtle p-5 text-sm text-content-tertiary">
                  {t("tasks.loading", "Loading…")}
                </div>
              ) : executions.length === 0 && !executionsQuery.isError ? (
                <div className="rounded-md border border-dashed border-edge-subtle p-5 text-sm text-content-tertiary">
                  {t("tasks.scheduled.noRuns", "No executions yet.")}
                </div>
              ) : (
                <div className="max-h-80 overflow-auto rounded-lg border border-edge-subtle">
                  {executions.map((execution) => (
                    <button
                      key={execution.id}
                      type="button"
                      onClick={() => setSelectedExecutionId(execution.id)}
                      aria-pressed={selectedExecution?.id === execution.id}
                      className={`grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 gap-y-1.5 border-b border-edge-subtle px-4 py-3 text-left text-sm last:border-b-0 hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus sm:grid-cols-[minmax(7rem,auto)_minmax(0,1fr)_auto] sm:items-center ${
                        selectedExecution?.id === execution.id ? "bg-focus/[0.06] font-medium ring-1 ring-inset ring-focus/20" : ""
                      }`}
                    >
                      <StatusBadge className="w-fit max-w-full" status={executionStatusTone(execution.status)}>
                        {t(`tasks.executionStatus.${execution.status}`, execution.status)}
                      </StatusBadge>
                      <span className="col-span-2 row-start-2 min-w-0 sm:col-span-1 sm:row-start-auto">
                        <span className="block break-words text-xs leading-5 text-muted-foreground">
                          {t(`tasks.trigger.${execution.trigger_type}`, execution.trigger_type)} · {formatTime(execution.triggered_at)}
                        </span>
                        {execution.error && (
                          <span className="mt-0.5 line-clamp-2 break-words text-xs text-destructive [overflow-wrap:anywhere]">
                            {execution.error}
                          </span>
                        )}
                      </span>
                      <span className="col-start-2 row-start-1 pt-1 font-mono text-xs text-muted-foreground sm:col-start-3 sm:pt-0">
                        {execution.id.slice(0, 8)}
                      </span>
                    </button>
                  ))}
                </div>
              )}
              {selectedExecution && (
                <div className="mt-4 border-t border-edge-subtle pt-4 text-xs">
                  <div className="flex flex-wrap items-center gap-2 font-medium text-foreground">
                    {t("tasks.scheduled.selectedRun", "Selected run")}{" "}
                    <span className="font-mono text-muted-foreground">
                      {selectedExecution.id.slice(0, 8)}
                    </span>
                    <CopyButton value={selectedExecution.id} />
                    <WorkflowVersionLink workflowId={selectedExecution.workflow_id} version={selectedExecution.version} kind="execution" />
                  </div>
                  <DetailSummary className="mt-4 sm:grid-cols-3" items={[
                    { label: t("taskDetail.startedAt", "Started"), value: formatTime(selectedExecution.started_at) },
                    { label: t("taskDetail.finishedAt", "Finished"), value: formatTime(selectedExecution.finished_at) },
                    { label: t("tasks.scheduled.notification", "Notification"), value: selectedExecution.notification_state?.status
                      ? t(`tasks.notificationStatus.${String(selectedExecution.notification_state.status)}`, String(selectedExecution.notification_state.status))
                      : "—" },
                  ]} />
                  {selectedExecution.error && (
                    <ActionableError
                      className="mt-4"
                      title={t("taskDetail.error", "Task execution failed")}
                      technicalDetails={<span className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{selectedExecution.error}</span>}
                    />
                  )}
                </div>
              )}
            </SectionBlock>
          )}
          <SectionBlock
            variant="plain"
            collapsible
            defaultOpen={false}
            title={t("taskDetail.events", "Events")}
            description={t("taskDetail.eventsDescription", "Live execution updates. Expand an event only when technical details are needed.")}
            contentClassName="min-w-0"
            actions={<span className="text-xs text-muted-foreground">
                {stream.done
                  ? t("taskDetail.streamClosed", "Stream closed")
                  : t("taskDetail.streamLive", "Live")}
              </span>}
          >
          <div className="mb-4" data-testid="task-event-filters">
          <LogHistoryControls
            value={logRange}
            order={logOrder}
            onValueChange={setLogRange}
            onOrderChange={setLogOrder}
          >
            <div className="min-w-40 flex-1 space-y-1.5 sm:max-w-56">
              <label className="text-xs font-medium text-content-secondary">
                {t("taskDetail.severity", "Severity")}
              </label>
              <Select value={levelFilter} onValueChange={(value) => setLevelFilter(value as (typeof LEVEL_OPTIONS)[number])}>
                <SelectTrigger aria-label={t("taskDetail.severity", "Severity")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {LEVEL_OPTIONS.map((level) => (
                    <SelectItem key={level} value={level}>
                      {t(`taskDetail.level.${level}`, level)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="min-w-40 flex-1 space-y-1.5 sm:max-w-56">
              <label className="text-xs font-medium text-content-secondary">
                {t("taskDetail.eventType", "Event type")}
              </label>
              <Select value={eventTypeFilter} onValueChange={(value) => setEventTypeFilter(value as TaskEventType | "all")}>
                <SelectTrigger aria-label={t("taskDetail.eventType", "Event type")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                {(["all", ...EVENT_TYPE_OPTIONS] as const).map((type) => (
                  <SelectItem key={type} value={type}>
                    {t(`taskDetail.eventType.${type}`, type)}
                  </SelectItem>
                ))}
                </SelectContent>
              </Select>
            </div>
            <span className="pb-2 text-xs text-content-tertiary">
              {t("logs.loadedVisible", "{{visible}} visible · {{loaded}} loaded", {
                visible: visibleEvents.length,
                loaded: allEvents.length,
              })}
            </span>
          </LogHistoryControls>
          </div>
          <div
            ref={eventLogRegionRef}
            className="app-scrollbar max-h-[65dvh] min-h-48 overflow-y-auto overscroll-contain pr-2"
            data-role="task-event-log-scroll-region"
          >
            {eventsQuery.isLoading ? (
              <div className="empty-state">{t("tasks.loading", "Loading…")}</div>
            ) : eventsQuery.isError ? (
              <ActionableError
                title={t("logs.loadError", "Failed to load logs.")}
                description={t("logs.loadErrorHint", "Check the connection and try again.")}
                actionLabel={t("retry", "Retry")}
                onAction={() => void eventsQuery.refetch()}
                technicalDetails={eventsQuery.error instanceof Error ? eventsQuery.error.message : undefined}
              />
            ) : allEvents.length === 0 ? (
              <div className="rounded border border-dashed p-6 text-center text-xs text-muted-foreground">
                {t("taskDetail.noEvents", "No events yet.")}
              </div>
            ) : visibleEvents.length === 0 ? (
              <div className="rounded border border-dashed border-edge-subtle p-6 text-center text-xs text-content-tertiary">
                {t("taskDetail.noMatchingEvents", "No events match these filters.")}
              </div>
            ) : (
              <ol>
                {visibleEvents.map((frame) => (
                  <EventRow
                    key={frame.id}
                    frame={frame}
                    formatTime={formatTime}
                    nowLabel={t("taskDetail.justNow", "Just now")}
                  />
                ))}
              </ol>
            )}
            <IncrementalLogLoader
              hasMore={Boolean(eventsQuery.hasNextPage)}
              loading={eventsQuery.isFetchingNextPage}
              onLoadMore={loadMoreEvents}
              order={logOrder}
              rootRef={eventLogRegionRef}
            />
          </div>
          </SectionBlock>
        </TabsContent>
        {!isScheduledRun && <TabsContent value="evaluation"><EvaluationTab taskId={task.id} canEdit={capabilities.has("update")} canExecute={capabilities.has("execute")} /></TabsContent>}
      </Tabs>
      <ResourceShareDialog
        open={shareOpen}
        onOpenChange={setShareOpen}
        resourceKind="task"
        resourceId={task.id}
        resourceName={`${t("taskDetail.title", "Task")} ${task.id.slice(0, 8)}`}
        effectiveRole={task.access?.effective_role}
        accessSource={task.access?.source}
      />
    </EntityDetailShell>
  );
}
