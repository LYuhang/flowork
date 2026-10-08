import { SectionBlock } from '@/components/layout/section-block';
import { CopyButton } from '@/components/ui/copy-button';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { StatusBadge, type SemanticStatus } from '@/components/ui/status';
import { useFormatDateTime } from '@/lib/timezone';
import { useExecutionHistory, type ExecutionSource, type ExecutionStatus } from '@/lib/api/queries/workflow-history';

const statuses: ExecutionStatus[] = ['waiting_approval', 'running', 'queued', 'succeeded', 'failed', 'timed_out', 'cancelled'];
const tones: Record<ExecutionStatus, SemanticStatus> = {
  queued: 'neutral', running: 'running', waiting_approval: 'warning', succeeded: 'success',
  failed: 'danger', timed_out: 'danger', cancelled: 'neutral',
};

/** One row per admitted input, regardless of the number of progress events. */
export function ExecutionHistory({ source, sourceId }: { source: ExecutionSource; sourceId: string }) {
  const { t } = useTranslation();
  const formatDateTime = useFormatDateTime();
  const [filter, setFilter] = useState<ExecutionStatus | 'all' | 'mine'>('all');
  const [now, setNow] = useState(Date.now);
  const query = useExecutionHistory(source, sourceId, filter);
  const items = useMemo(() => {
    const records = query.data?.pages.flatMap((page) => page.items.map((item) => ({
      ...item, serverTime: page.server_time, receivedAt: page.received_at,
    }))) ?? [];
    const unique = new Map<string, typeof records[number]>();
    for (const item of records) if (!unique.has(item.id)) unique.set(item.id, item);
    return [...unique.values()];
  }, [query.data]);
  const hasPending = items.some((item) => item.pending_approvals?.length);
  useEffect(() => {
    if (!hasPending) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasPending]);
  return (
    <SectionBlock title={t('execution.history')}
      actions={
        <div className="flex items-center gap-2">
          <Select value={filter} onValueChange={(value) => setFilter(value as typeof filter)}>
            <SelectTrigger className="w-44" aria-label={t('execution.filter')}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('execution.all')}</SelectItem>
              <SelectItem value="mine">{t('execution.mine')}</SelectItem>
              {statuses.map((status) => <SelectItem key={status} value={status}>{t(`execution.status.${status}`)}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button size="sm" variant="outline" aria-label={t('execution.refreshHistory')} disabled={query.isFetching} onClick={() => void query.refetch()}>{t('execution.refresh')}</Button>
        </div>
      }
    >
      {query.isError && <p role="alert" className="text-sm text-state-danger">{t('execution.unavailable')}</p>}
      {query.isError && items.length === 0 ? null : query.isLoading ? <p role="status" className="text-sm text-content-secondary">{t('execution.loading')}</p>
          : items.length === 0 ? <p className="text-sm text-content-secondary">{t('execution.empty')}</p>
            : <div className="app-scrollbar max-h-[32rem] overflow-auto rounded-lg border border-edge-subtle">
              <table data-slot="execution-table" className="w-full text-left text-sm" aria-label={t('execution.history')}>
                <thead className="sticky top-0 z-10 bg-surface-sunken">
                  <tr>
                    <th scope="col" className="px-4 py-3">{t('execution.id')}</th>
                    <th scope="col" className="px-4 py-3">{t('tasks.col.status', 'Status')}</th>
                    <th scope="col" className="px-4 py-3">{t('taskDetail.submittedAt', 'Submitted')}</th>
                    <th scope="col" className="px-4 py-3"><span className="sr-only">{t('execution.detail')}</span></th>
                  </tr>
                </thead>
                <tbody>
              {items.map((item) => <tr key={item.id}>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-1">
                    <span className="max-w-[16rem] truncate font-mono text-xs" title={item.id}>{item.id}</span>
                    <CopyButton value={item.id} />
                  </div>
                  {item.input_index != null && <span className="text-xs text-content-secondary">{t('execution.inputIndex', { index: item.input_index + 1 })}</span>}
                </td>
                <td className="px-4 py-3">
                  <StatusBadge status={tones[item.status]}>{t(`execution.status.${item.status}`)}</StatusBadge>
                  {item.pending_approvals?.map((approval) => {
                    const serverNow = item.serverTime && item.receivedAt
                      ? Date.parse(item.serverTime) + Math.max(0, now - item.receivedAt) : now;
                    const seconds = Math.max(0, Math.ceil((Date.parse(approval.deadline) - serverNow) / 1000));
                    const time = [Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60), seconds % 60]
                      .map((part) => String(part).padStart(2, '0')).join(':');
                    return <p key={approval.id} className="mt-1 break-words text-xs text-content-secondary">
                      {approval.approver_email ? `${t('approval.approverEmail')}: ${approval.approver_email}` : t('approval.waitingForAssignee')}
                      {' · '}<span role="timer">{seconds > 0 ? t('approval.remaining', { time }) : t('approval.expiring')}</span>
                    </p>;
                  })}
                </td>
                <td className="whitespace-nowrap px-4 py-3"><time dateTime={item.created_at}>{formatDateTime(item.created_at)}</time></td>
                <td className="whitespace-nowrap px-4 py-3 text-right">
                <Link className="shrink-0 text-sm font-medium text-primary underline underline-offset-4" to={`/workflow-executions/${item.id}`}>
                  {t('execution.detail')}
                </Link>
                </td>
              </tr>)}
                </tbody>
              </table>
            </div>}
      {query.hasNextPage && <Button variant="outline" size="sm" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>{t('execution.loadMore')}</Button>}
    </SectionBlock>
  );
}
