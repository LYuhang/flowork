import { WorkflowVersionLink } from '@/components/resources/WorkflowVersionLink';
import { useTranslation } from 'react-i18next';
import type { Deployment } from '@/lib/api/deployments';
import { useFormatDateTime } from '@/lib/timezone';
import { CopyButton } from '@/components/ui/copy-button';
import { StatusBadge } from '@/components/ui/status';
import { SectionBlock } from '@/components/layout/section-block';

export function DeploymentInstances({ dep }: { dep: Deployment }) {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  if (!dep.runtime) return null;
  return <SectionBlock title={t('deployments.runtime.title', 'Runtime instances')}
    description={t('deployments.runtime.description', 'Saved settings take effect after the new instance is ready and traffic has switched.')}>
    <p role="status" className="mb-4 text-sm text-content-secondary">
      {t('deployments.runtime.rollout', 'Deployment state')}: {t(`deployments.runtime.${dep.rollout_status ?? 'pending'}`, dep.rollout_status ?? 'pending')}
    </p>
    {dep.runtime.instances.length === 0 ? <p className="rounded-lg border border-dashed border-edge-subtle p-5 text-sm text-content-tertiary">
      {t('deployments.runtime.empty', 'No resident instance is running.')}
    </p> : <ul className="space-y-3">
      {dep.runtime.instances.map(instance => <li key={instance.id} className="min-w-0 rounded-xl border border-edge-subtle p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate font-mono text-xs" title={instance.id}>{instance.id}</span>
            <CopyButton value={instance.id} className="shrink-0" />
          </div>
          <StatusBadge status={instance.state === 'active' ? 'success' : instance.state === 'draining' ? 'warning' : 'running'}>
            {t(`deployments.runtime.${instance.state}`, { active: 'Serving', preparing: 'Starting', draining: 'Draining requests' }[instance.state])}
          </StatusBadge>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 text-sm sm:grid-cols-4">
          <div><dt className="text-xs text-content-tertiary">{t('tasks.version.label', 'Workflow version')}</dt><dd className="mt-1 font-mono"><WorkflowVersionLink workflowId={dep.wf_id} version={instance.version} inline /></dd></div>
          <div><dt className="text-xs text-content-tertiary">{t('deployments.workers.runtime', 'Workers × concurrency')}</dt><dd className="mt-1 tabular-nums">{instance.worker_count ?? 1} × {(instance.worker_concurrency ?? -1) === -1 ? t('deployments.workers.unlimited', 'Unlimited') : (instance.worker_concurrency ?? -1)}</dd></div>
          <div><dt className="text-xs text-content-tertiary">/mount</dt><dd className="mt-1">{instance.mount_enabled ? t('deployments.settings.on', 'On') : t('deployments.settings.off', 'Off')}</dd></div>
          <div><dt className="text-xs text-content-tertiary">{t('deployments.runtime.pendingRequests', 'Unfinished requests')}</dt><dd className="mt-1 tabular-nums">{instance.pending_requests}</dd></div>
          <div><dt className="text-xs text-content-tertiary">{t('deployments.runtime.activatedAt', 'Serving since')}</dt><dd className="mt-1 break-words tabular-nums">{instance.activated_at ? formatTime(instance.activated_at) : '—'}</dd></div>
          <div><dt className="text-xs text-content-tertiary">{t('deployments.resources.cpuUsage', 'CPU usage · 100% = 1 core')}</dt><dd className="mt-1 tabular-nums">{instance.metrics?.cpu_percent == null ? '—' : `${instance.metrics.cpu_percent.toFixed(1)}%`}</dd></div>
          <div><dt className="text-xs text-content-tertiary">{t('deployments.resources.memoryUsage', 'Memory usage / limit')}</dt><dd className="mt-1 tabular-nums">{instance.metrics?.memory_bytes == null ? '—' : `${(instance.metrics.memory_bytes / 1048576).toFixed(0)} / ${instance.metrics.memory_mb ?? '—'} MiB`}</dd></div>
          <div><dt className="text-xs text-content-tertiary">{t('deployments.resources.uptime', 'Uptime (minutes)')}</dt><dd className="mt-1 tabular-nums">{instance.metrics?.uptime_seconds == null ? '—' : Math.floor(instance.metrics.uptime_seconds / 60)}</dd></div>
          <div><dt className="text-xs text-content-tertiary">{t('deployments.resources.observedAt', 'Last observed')}</dt><dd className="mt-1 break-words tabular-nums">{instance.observed_at ? formatTime(instance.observed_at) : '—'}</dd></div>
        </dl>
      </li>)}
    </ul>}
  </SectionBlock>;
}
