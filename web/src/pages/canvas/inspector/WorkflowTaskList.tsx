import { useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useWorkflowTasks } from '@/lib/api/queries/tasks';
import { type TaskType } from '@/lib/api/tasks';
import { CopyButton } from '@/components/agent-sidebar/tool-render/CopyButton';
import { Button } from '@/components/ui/button';

export function WorkflowTaskList({ wfId, taskType, active = true, onOpen }: {
  wfId: string; taskType: TaskType; active?: boolean; onOpen?: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [offset, setOffset] = useState(0);
  const query = useWorkflowTasks(wfId, active, taskType, offset);
  const tasks = query.data?.items ?? [];
  return <section className="space-y-3 border-t border-edge-subtle pt-3" data-testid="workflow-task-list">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-medium">{t('tasks.related.title', 'Related tasks · all versions')}</h3>
      <Link to={`/tasks?workflow_id=${encodeURIComponent(wfId)}&type=${taskType}`} className="text-xs text-primary hover:underline" data-testid="batch-view-task-center">
        {t('canvas.batch.viewInTaskCenter', 'View in Task Center')}
      </Link>
    </div>
    {query.isError ? <Button variant="outline" onClick={() => void query.refetch()}>{t('common.retry', 'Retry')}</Button>
      : query.isLoading ? <p className="text-xs">{t('tasks.loading', 'Loading…')}</p>
      : !tasks.length ? <p className="text-xs text-muted-foreground" data-testid="batch-no-tasks">{t('tasks.related.empty', 'No related tasks yet.')}</p>
      : <ul className="space-y-2" data-testid="batch-task-list">{tasks.map(task => <li key={task.id} className="rounded-md border border-edge-subtle p-2.5 space-y-2">
        <div className="flex items-center gap-1 min-w-0">
          {onOpen ? <button type="button" className="min-w-0 truncate font-mono text-xs text-left hover:underline" title={task.id} onClick={() => onOpen?.(task.id)} data-testid="batch-task-row" data-task-id={task.id}>{task.id}</button> : <span className="min-w-0 truncate font-mono text-xs" title={task.id}>{task.id}</span>}
          <CopyButton value={task.id} label={t('tasks.related.copyId', 'Copy ID')} className="shrink-0" />
          <Link to={`/tasks/${task.id}`} className="shrink-0 text-xs text-primary hover:underline">{t('tasks.related.details', 'Details ↗')}</Link>
        </div>
        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
          <span>{task.workflow_version ?? (task.payload as { workflow_snapshot?: { version?: string } } | null)?.workflow_snapshot?.version ?? '—'}</span>
          <span data-testid="batch-task-status">{t(`tasks.status.${task.status}`, task.status)}</span>
        </div>
      </li>)}</ul>}
    {(offset > 0 || offset + tasks.length < (query.data?.total ?? 0)) && <div className="flex items-center justify-end gap-2">
      <Button size="sm" variant="ghost" disabled={offset === 0 || query.isFetching} onClick={() => setOffset(Math.max(0, offset - 10))}>{t('common.previous', 'Previous')}</Button>
      <Button size="sm" variant="ghost" disabled={offset + tasks.length >= (query.data?.total ?? 0) || query.isFetching} onClick={() => setOffset(offset + 10)}>{t('common.next', 'Next')}</Button>
    </div>}
  </section>;
}
