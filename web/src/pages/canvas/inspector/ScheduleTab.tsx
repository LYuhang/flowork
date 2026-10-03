import { WorkflowTaskList } from './WorkflowTaskList';
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useFormatDateTime } from '@/lib/timezone';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useUIStore } from '@/stores/ui';
import { useCommitWorkflow } from '@/lib/api/mutations/workflow-ops';
import { ScheduledRunCreatePanel } from '@/pages/tasks/ScheduledRunCreatePanel';
import { useExecStreamStore } from '@/stores/exec-stream';
import type { ScheduledRunResponse } from '@/lib/api/tasks';

export function ScheduleTab({ wfId }: { wfId: string }) {
  const qc = useQueryClient();
  const draft = useWorkflowEditStore(s => s.draft);
  const version = useWorkflowEditStore(s => s.baseVersion);
  const dirty = useWorkflowEditStore(s => s.dirty);
  const commit = useCommitWorkflow(wfId);
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const [created, setCreated] = useState<ScheduledRunResponse | null>(null);
  if (!draft || !version) return null;
  const relatedTasks = <WorkflowTaskList key={wfId} wfId={wfId} taskType="scheduled_run" />;
  if (created) return <div className="min-h-0 overflow-y-auto space-y-3 p-4">
    <p>{t('tasks.scheduled.created', 'Scheduled run created')}</p>
    <p className="text-sm">{created.schedule.name} · {created.schedule.workflow_selector?.version}</p>
    <p className="text-xs text-muted-foreground">{created.schedule.schedule_type === 'once'
      ? formatTime(created.schedule.run_at, { timeZone: created.schedule.timezone, timeStyle: 'medium' }) : created.schedule.schedule_type === 'interval'
        ? `${created.schedule.interval_seconds}s` : created.schedule.cron_expr} · {created.schedule.timezone}</p>
    <Button asChild><Link to={`/tasks/${created.task.id}`}>{t('canvas.batch.viewTask', 'View task')}</Link></Button>
    <Button variant="outline" onClick={() => setCreated(null)}>{t('tasks.scheduled.createAnother', 'Create another')}</Button>
    {relatedTasks}
  </div>;
  return <ScheduledRunCreatePanel
    relatedTasks={relatedTasks}
    key={wfId}
    onCancel={() => useUIStore.getState().setInspectorOpen(false)}
    onCreated={(_id, response) => { setCreated(response); void qc.invalidateQueries({ queryKey: ["tasks"] }); }}
    context={{ workflowId: wfId, version, workflow: draft, dirty,
      initialInputs: useExecStreamStore.getState().inputsByWorkflow[wfId] ?? {},
      name: String((draft.__meta__ as Record<string, unknown> | undefined)?.workflow_name ?? wfId),
      prepare: async () => {
        const state = useWorkflowEditStore.getState();
        if (state.baseVersion !== version || !state.draft) throw new Error(t('tasks.scheduled.versionChanged', 'The displayed version changed. Review the task configuration.'));
        if (!state.dirty) return version;
        const saved = await commit.mutateAsync(state.draft);
        return `v${saved.active_v}.sv${saved.active_sv}`;
      },
    }}
  />;
}
