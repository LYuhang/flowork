/**
 * Workflow-scoped task list query.
 *
 * The Batch Inspector tab is a WORKFLOW-SCOPED lens onto the same `tasks`
 * table the global `/tasks` Task Center reads — filtered to one workflow via
 * `listTasks({ workflow_id })` (the route already supports the filter:
 * `routes/tasks.py`). This is a clean SUBSET of the Task Center, not a
 * duplicate: the tab lists THIS workflow's batch runs (status/version) so the
 * user can watch a just-submitted run without leaving the canvas, and a "View
 * in Task Center" link hands off to the cross-workflow view.
 *
 * Polling: a 5s `refetchInterval` keeps the in-tab list fresh while a batch
 * is queued/running. The detail SSE (reused from the Task Center) owns the
 * live per-row progress once a task is opened; this list owns the canonical
 * status snapshot — mirrors `TaskDetailPage`'s two-channel split.
 */
import { useQuery } from '@tanstack/react-query';
import { listTasks, type TaskType, type TaskListResponse } from '@/lib/api/tasks';

const POLL_INTERVAL_MS = 5_000;

/**
 * The batch tasks for a single workflow, newest-first (the route orders by
 * `submitted_at desc`). Disabled until a real `wfId`. Polls while mounted so a
 * freshly-submitted run appears + advances without a manual refresh.
 */
export function useWorkflowTasks(wfId: string | undefined, enabled = true, taskType: TaskType = 'batch_exec', offset = 0) {
  return useQuery<TaskListResponse>({
    queryKey: ['tasks', 'workflow', wfId, taskType, offset],
    queryFn: () => listTasks({ workflow_id: wfId, task_type: [taskType], limit: 10, offset }),
    enabled: !!wfId && enabled,
    refetchInterval: enabled ? POLL_INTERVAL_MS : false,
    refetchOnWindowFocus: false,
  });
}
