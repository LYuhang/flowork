import { useState } from 'react';
import { Link } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CreateDeploymentModal } from '@/pages/deployments/CreateDeploymentModal';
import { listDeployments } from '@/lib/api/deployments';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useUIStore } from '@/stores/ui';
import { useCommitWorkflow } from '@/lib/api/mutations/workflow-ops';
import { CopyButton } from '@/components/agent-sidebar/tool-render/CopyButton';
import { Button } from '@/components/ui/button';

export function DeploymentTab({ wfId, active }: { wfId: string; active: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const draft = useWorkflowEditStore(s => s.draft);
  const version = useWorkflowEditStore(s => s.baseVersion);
  const dirty = useWorkflowEditStore(s => s.dirty);
  const commit = useCommitWorkflow(wfId);
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ['deployments', 'workflow', wfId, offset],
    queryFn: () => listDeployments({ workflow_id: wfId, limit: 10, offset }),
    enabled: active,
    refetchInterval: active ? 5000 : false,
  });
  if (!draft || !version) return null;
  const items = query.data?.items ?? [];
  return <div className="space-y-5 pt-3" data-testid="workflow-deployment-panel">
    <CreateDeploymentModal open inline initialWorkflowId={wfId}
      initialName={`${String((draft.__meta__ as Record<string, unknown> | undefined)?.workflow_name ?? wfId)} API`}
      onOpenChange={open => { if (!open) useUIStore.getState().setInspectorOpen(false); }}
      onCreated={() => { setOffset(0); void qc.invalidateQueries({ queryKey: ['deployments'] }); }}
      context={{ version, dirty, prepare: async () => {
        const state = useWorkflowEditStore.getState();
        if (state.baseVersion !== version || !state.draft) throw new Error(t('tasks.scheduled.versionChanged', 'The displayed version changed. Review the task configuration.'));
        if (!state.dirty) return version;
        const saved = await commit.mutateAsync(state.draft);
        return `v${saved.active_v}.sv${saved.active_sv}`;
      } }} />
    <section className="border-t pt-3 space-y-3" data-testid="workflow-deployment-list">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">{t('deployments.workflow.related', 'Related deployments · all versions')}</h3>
        <Link className="text-xs text-primary hover:underline" to={`/deployments?workflow_id=${encodeURIComponent(wfId)}`}>{t('deployments.workflow.viewAll', 'View all')}</Link>
      </div>
      {query.isError ? <Button variant="outline" onClick={() => void query.refetch()}>{t('common.retry', 'Retry')}</Button> : query.isLoading ? <p className="text-xs">{t('tasks.loading', 'Loading…')}</p> : !items.length ? <p className="text-xs text-muted-foreground">{t('deployments.workflow.empty', 'No related deployments yet.')}</p> :
        <ul className="space-y-2">{items.map(dep => <li key={dep.id} className="rounded-md border p-2.5 space-y-2" data-deployment-id={dep.id}>
          <div className="flex justify-between gap-2 text-sm"><span className="truncate" title={dep.name}>{dep.name}</span><span className="text-xs text-muted-foreground">{dep.trigger_type.toUpperCase()}</span></div>
          <div className="flex items-center gap-1">
            <span className="min-w-0 truncate font-mono text-xs" title={dep.id}>{dep.id}</span>
            <CopyButton value={dep.id} label={t('tasks.related.copyId', 'Copy ID')} className="shrink-0" />
            <Link className="shrink-0 text-xs text-primary hover:underline" to={`/deployments/${dep.id}`}>{t('tasks.related.details', 'Details ↗')}</Link>
          </div>
          <div className="flex justify-between gap-2 text-xs text-muted-foreground">
            <span>{`v${dep.pinned_major}.sv${dep.pinned_sub}`}</span>
            <span>{!dep.enabled ? t('deployments.status.disabled', 'Disabled') : dep.rollout_status === 'ready' && dep.active_version ? t('deployments.workflow.running', 'Running') : t(`deployments.runtime.${dep.rollout_status || 'pending'}`, dep.rollout_status || 'pending')}</span>
          </div>
          {dep.enabled && dep.active_version && (dep.rollout_status !== 'ready' || dep.active_version !== `v${dep.pinned_major}.sv${dep.pinned_sub}`) && <p className="text-xs text-muted-foreground">{t('deployments.workflow.activeVersion', 'Serving version')}: {dep.active_version}</p>}
        </li>)}</ul>}
      {(offset > 0 || offset + items.length < (query.data?.total ?? 0)) && <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" disabled={offset === 0 || query.isFetching} onClick={() => setOffset(Math.max(0, offset - 10))}>{t('common.previous', 'Previous')}</Button>
        <Button size="sm" variant="ghost" disabled={offset + items.length >= (query.data?.total ?? 0) || query.isFetching} onClick={() => setOffset(offset + 10)}>{t('common.next', 'Next')}</Button>
      </div>}
    </section>
  </div>;
}
