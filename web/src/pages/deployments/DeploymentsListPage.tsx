import { ResourceSort } from '@/components/resources/ResourceSort';
import { useResourceSort } from '@/lib/resource-sort';
import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { toast } from 'sonner';
import {
  MoreHorizontal,
  PlugZap,
  RefreshCw,
  Rocket,
  Search,
  Share2,
  ShieldCheck,
  Trash2,
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
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  listDeployments,
  patchDeployment,
  deleteDeployment,
  type Deployment,
  type TriggerType,
} from '@/lib/api/deployments';
import { useFormatDateTime } from '@/lib/timezone';
import { cn } from '@/lib/utils';
import { useWorkspaceList } from '@/lib/api/queries/workflows';
import { SearchSelect } from '@/components/ui/search-select';
import { CreateDeploymentModal } from '@/pages/deployments/CreateDeploymentModal';
import { ManagementPageShell, ManagementToolbar } from '@/components/layout/management-page-shell';
import { OperationalSummary } from '@/components/layout/operational-summary';
import { ResourceShareDialog } from '@/components/modals/ResourceShareDialog';
import { CopyButton } from '@/components/ui/copy-button';
import { StatusBadge } from '@/components/ui/status';
import { formatNumber } from '@/lib/format/number';
import { ActionableError } from '@/components/presentation/ActionableError';
import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { AsyncState } from '@/components/ui/async-state';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import { WorkflowPagination } from '@/pages/workspace/WorkflowPagination';


type DeploymentKind = TriggerType;
type StatusFilter = 'all' | 'active' | 'disabled';

const DEPLOYMENT_TYPES: DeploymentKind[] = ['api', 'webhook'];
const SORT_FIELDS = ["created_at", "updated_at", "last_invoked_at"] as const;
const PAGE_SIZE = 25;

function triggerLabel(type: TriggerType, t: TFunction): string {
  if (type === 'api') return 'API';
  return t('deployments.type.webhook', 'Webhook');
}

function endpointFor(dep: Deployment): string {
  return dep.trigger_type === 'webhook'
    ? `/api/v1/deployments/${dep.slug}/webhook`
    : `/api/v1/deployments/${dep.slug}/invoke`;
}

export function DeploymentsListPage() {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const qc = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const sorting = useResourceSort(SORT_FIELDS, 'created_at');
  const resourceScope: 'all' | 'created' | 'shared' = searchParams.get('scope') === 'shared'
    ? 'shared' : searchParams.get('scope') === 'created' ? 'created' : 'all';
  const typeParam = searchParams.get('type');
  const typeFilter: 'all' | DeploymentKind = DEPLOYMENT_TYPES.includes(typeParam as DeploymentKind)
    ? typeParam as DeploymentKind
    : 'all';
  const statusParam = searchParams.get('status');
  const statusFilter: StatusFilter = statusParam === 'active' || statusParam === 'disabled'
    ? statusParam
    : 'all';
  const search = searchParams.get('q') ?? '';
  const rawPage = Number.parseInt(searchParams.get('page') ?? '0', 10);
  const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 0;
  const [searchDraft, setSearchDraft] = useState(search);
  const [createOpen, setCreateOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<Deployment | null>(null);
  const [confirmEnabled, setConfirmEnabled] = useState<{
    deployment: Deployment;
    enabled: boolean;
  } | null>(null);
  const [shareTarget, setShareTarget] = useState<Deployment | null>(null);
  const initialWorkflowId = searchParams.get('workflow_id') ?? '';
  const workflowsQuery = useWorkspaceList(200, 0);
  const initialWorkflowName = searchParams.get('workflow_name') ?? '';
  const initialDeploymentName = initialWorkflowName ? `${initialWorkflowName} API` : '';

  const effectiveCreateOpen = createOpen || searchParams.get('create') === '1';

  const updateListParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(updates)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setSearchParams(next, { replace: true });
  };

  useEffect(() => {
    // Browser navigation may replace the query string externally; mirror it
    // into the debounced input before accepting further edits.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSearchDraft(search);
  }, [search]);

  useEffect(() => {
    if (searchDraft === search) return;
    const timer = window.setTimeout(() => {
      const next = new URLSearchParams(searchParams);
      if (searchDraft.trim()) next.set('q', searchDraft.trim());
      else next.delete('q');
      next.delete('page');
      setSearchParams(next, { replace: true });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search, searchDraft, searchParams, setSearchParams]);

  const updateCreateOpen = (open: boolean) => {
    setCreateOpen(open);
    if (!open && searchParams.get('create') === '1') {
      const next = new URLSearchParams(searchParams);
      next.delete('create');
      next.delete('workflow_name');
      setSearchParams(next, { replace: true });
    }
  };

  const query = useQuery({
    queryKey: ['deployments', { search, typeFilter, statusFilter, page, workflowId: initialWorkflowId, resourceScope, sortBy: sorting.field, sortOrder: sorting.direction }],
    queryFn: () => listDeployments({
      source: resourceScope,
      sort_by: sorting.field,
      sort_order: sorting.direction,
      q: search || undefined,
      workflow_id: initialWorkflowId || undefined,
      trigger_type: typeFilter === 'all' ? undefined : typeFilter,
      enabled: statusFilter === 'all' ? undefined : statusFilter === 'active',
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }),
    placeholderData: (previous) => previous,
    refetchOnWindowFocus: false,
    refetchInterval: 15_000,
  });

  const enabledMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      patchDeployment(id, { enabled }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['deployments'] });
      setConfirmEnabled(null);
      toast.success(t('deployments.statusUpdated', 'Deployment status updated'));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteDeployment(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['deployments'] });
      setConfirmDelete(null);
      toast.success(t('deployments.deleted', 'Deployment deleted'));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  const filtered = useMemo(() => query.data?.items ?? [], [query.data?.items]);
  const total = query.data?.total ?? filtered.length;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const summary = query.data?.summary;
  const activeCount = summary?.active ?? filtered.filter((dep) => dep.enabled).length;
  const disabledCount = summary?.disabled ?? filtered.filter((dep) => !dep.enabled).length;
  const invokeCount = summary?.invocations
    ?? filtered.reduce((acc, dep) => acc + (dep.invoke_count ?? 0), 0);
  const lastActivity = summary?.last_invoked_at
    ?? filtered
      .map((dep) => dep.last_invoked_at)
      .filter((value): value is string => Boolean(value))
      .sort()
      .at(-1)
    ?? null;

  const setResourceScope = (value: string) => updateListParams({
    scope: value === 'all' ? null : value,
    page: null,
  });

  return (
    <>
      <ManagementPageShell
        resourceKind="deployment"
        title={t('deployments.title', 'Deployment')}
        description={t('deployments.subtitle', 'Publish workflows as APIs or webhooks.')}
        icon={Rocket}
        className="gap-5"
        actions={<>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void query.refetch()}
              disabled={query.isFetching}
            >
              <RefreshCw className={cn('mr-2 h-4 w-4', query.isFetching && 'animate-spin')} aria-hidden="true" />
              {t('refresh', 'Refresh')}
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              {t('deployments.new', 'New deployment')}
            </Button>
          </>}
      >

        <OperationalSummary
          label={t('deployments.summary.label', 'Deployment status summary')}
          items={[
            { icon: ShieldCheck, label: t('deployments.summary.active', 'Active'), value: activeCount, tone: 'success', hint: t('deployments.summaryHint.active', 'Deployments accepting requests.') },
            { icon: PlugZap, label: t('deployments.summary.disabled', 'Disabled'), value: disabledCount, tone: 'neutral', hint: t('deployments.summaryHint.disabled', 'Deployments kept but not serving traffic.') },
            { icon: Rocket, label: t('deployments.summary.invokes', 'Total calls'), value: invokeCount, tone: 'info', hint: t('deployments.summaryHint.invokes', 'Recorded API, webhook, and test invocations.') },
            { icon: RefreshCw, label: t('deployments.summary.lastActivity', 'Last activity'), value: formatTime(lastActivity), tone: 'neutral', hint: t('deployments.summaryHint.lastActivity', 'Most recent API, webhook, or test request.') },
          ]}
        />

        <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-edge-structural bg-surface-work">
          <ManagementToolbar className="flex-col items-stretch border-x-0 border-t-0 bg-surface-sunken/70 px-4 py-3">
            <div className="relative w-full min-w-0 sm:max-w-md">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
                placeholder={t('deployments.search', 'Search name, endpoint, workflow, or id')}
                aria-label={t('deployments.search', 'Search name, endpoint, workflow, or id')}
                name="deployment-search"
                autoComplete="off"
                className="pl-9"
              />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Select value={resourceScope} onValueChange={setResourceScope}>
                <SelectTrigger className="w-48" aria-label={t('skills.relationship.label', 'Resource relationship')}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('skills.relationship.all', 'All relationships')}</SelectItem>
                  <SelectItem value="created">{t('skills.relationship.created', 'Created by me')}</SelectItem>
                  <SelectItem value="shared">{t('skills.relationship.shared', 'Shared with me')}</SelectItem>
                </SelectContent>
              </Select>
              <SearchSelect value={initialWorkflowId} onValueChange={value => updateListParams({ workflow_id: value || null, page: null })}
                options={(workflowsQuery.data?.items ?? []).map(w => ({ value: w.wf_id, label: w.workflow_name || w.wf_id, meta: w.wf_id }))
                  .concat(initialWorkflowId && !workflowsQuery.data?.items.some(w => w.wf_id === initialWorkflowId) ? [{ value: initialWorkflowId, label: initialWorkflowId, meta: initialWorkflowId }] : [])}
                placeholder={t('tasks.related.filter', 'Filter by Workflow')} searchPlaceholder={t('tasks.related.filter', 'Filter by Workflow')} />
              {initialWorkflowId && <Button variant="ghost" size="sm" onClick={() => updateListParams({ workflow_id: null, page: null })}>{t('tasks.related.clear', 'All workflows')}</Button>}
              <Select value={typeFilter} onValueChange={(value) => updateListParams({ type: value === 'all' ? null : value, page: null })}>
                <SelectTrigger className="w-[150px]" aria-label={t('deployments.filter.type', 'Filter by deployment type')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('deployments.filter.allTypes', 'All types')}</SelectItem>
                  {DEPLOYMENT_TYPES.map((type) => (
                    <SelectItem key={type} value={type}>
                      {triggerLabel(type, t)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={statusFilter} onValueChange={(value) => updateListParams({ status: value === 'all' ? null : value, page: null })}>
                <SelectTrigger className="w-[150px]" aria-label={t('deployments.filter.status', 'Filter by deployment status')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('deployments.filter.allStatuses', 'All status')}</SelectItem>
                  <SelectItem value="active">{t('deployments.status.active', 'Active')}</SelectItem>
                  <SelectItem value="disabled">{t('deployments.status.disabled', 'Disabled')}</SelectItem>
                </SelectContent>
              </Select>
              <ResourceSort fields={SORT_FIELDS} value={sorting.value} onValueChange={sorting.setValue} />
            </div>
          </ManagementToolbar>

          {query.isLoading ? (
            <AsyncState kind="loading" title={t('tasks.loading', 'Loading…')} className="m-4" />
          ) : query.isError ? (
            <ActionableError
              className="m-4"
              title={t('deployments.load_error', 'Failed to load deployments.')}
              description={t('deployments.load_error_hint', 'Check your connection, then reload the deployment list.')}
              actionLabel={t('retry', 'Retry')}
              onAction={() => void query.refetch()}
              technicalDetails={query.error instanceof Error ? query.error.message : String(query.error)}
              technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
            />
          ) : filtered.length === 0 ? (
            <CompactEmptyState
              title={total === 0 && !search && typeFilter === 'all' && statusFilter === 'all'
                ? t('deployments.empty', 'No deployments yet.')
                : t('deployments.emptyFiltered', 'No deployments match the current filters.')}
              description={t(
                'deployments.emptyHint',
                'Create an API or webhook deployment from a workflow.',
              )}
            />
          ) : (
            <div className="resource-list-container app-scrollbar min-h-0 flex-1 overflow-auto" data-testid="deployments-table-scroll">
              <table role="table" className="resource-table resource-table-deployments w-full text-sm">
                <thead className="sticky top-0 z-10 border-b bg-surface-sunken text-left text-xs font-medium text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 font-medium">{t('deployments.col.name', 'Name')}</th>
                    <th className="px-4 py-3 font-medium">{t('deployments.col.type', 'Type')}</th>
                    <th className="px-4 py-3 font-medium">{t('deployments.col.status', 'Status')}</th>
                    <th className="px-4 py-3 font-medium">{t('deployments.col.workflow', 'Workflow')}</th>
                    <th className="px-4 py-3 text-right font-medium">{t('deployments.col.invokes', 'Calls')}</th>
                    <th className="px-4 py-3 font-medium">{t('deployments.col.lastActivity', 'Last activity')}</th>
                    <th className="px-4 py-3 text-right font-medium">{t('deployments.col.actions', 'Actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((dep) => {
                    const endpoint = endpointFor(dep);
                    const capabilities = new Set(dep.access?.capabilities ?? []);
                    return (
                      <tr key={dep.id} className="border-b last:border-b-0 hover:bg-muted/30">
                        <td data-col="name" className="px-4 py-3">
                          <div className="flex min-w-0 items-start gap-2.5">
                            <ResourceIcon kind="deployment" size="sm" />
                            <div className="min-w-0 flex-1">
                              <Link
                                to={`/deployments/${dep.id}`}
                                title={dep.name}
                                className="block truncate font-medium text-foreground underline-offset-4 hover:text-primary hover:underline"
                              >
                                {dep.name}
                              </Link>
                              <div className="mt-1 flex max-w-[360px] items-center gap-1.5 text-xs text-muted-foreground">
                                <code className="min-w-0 flex-1 truncate font-mono" title={endpoint} translate="no">{endpoint}</code>
                                <CopyButton
                                  value={endpoint}
                                  label={t('deployments.actions.copyEndpoint', 'Copy endpoint')}
                                  copiedLabel={t('deployments.actions.copied', 'Copied')}
                                />
                              </div>
                              <ResourceProvenanceLine provenance={dep.provenance} className="mt-0.5 flex" />
                            </div>
                          </div>
                        </td>
                        <td data-col="type" data-label={t('deployments.col.type', 'Type')} className="px-4 py-3">
                          <StatusBadge status="neutral" showDot={false}>
                            {triggerLabel(dep.trigger_type, t)}
                          </StatusBadge>
                        </td>
                        <td data-col="status" data-label={t('deployments.col.status', 'Status')} className="px-4 py-3">
                          <StatusBadge status={dep.enabled ? 'success' : 'neutral'}>
                            {dep.enabled
                              ? t('deployments.status.active', 'Active')
                              : t('deployments.status.disabled', 'Disabled')}
                          </StatusBadge>
                        </td>
                        <td data-col="workflow" data-label={t('deployments.col.workflow', 'Workflow')} className="max-w-[180px] truncate px-4 py-3 font-mono text-xs text-muted-foreground" title={dep.wf_id}>
                          {dep.wf_id}
                        </td>
                        <td data-col="invokes" data-label={t('deployments.col.invokes', 'Calls')} className="px-4 py-3 text-right tabular-nums text-muted-foreground">
                          {formatNumber(dep.invoke_count)}
                        </td>
                        <td title={formatTime(dep.last_invoked_at)} data-col="lastActivity" data-label={t('deployments.col.lastActivity', 'Last activity')} className="px-4 py-3 text-muted-foreground">
                          {formatTime(dep.last_invoked_at)}
                        </td>
                        <td data-col="actions" className="px-4 py-3 text-right">
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8"
                                aria-label={t('open_actions_menu', 'Open actions menu for {{name}}', {
                                  name: dep.name,
                                })}
                                title={t('open_actions_menu', 'Open actions menu for {{name}}', {
                                  name: dep.name,
                                })}
                              >
                                <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem asChild>
                                <Link to={`/deployments/${dep.id}`}>
                                  {t('deployments.actions.detail', 'Open detail')}
                                </Link>
                              </DropdownMenuItem>
                              {capabilities.has('update') ? (
                                <DropdownMenuItem
                                  onClick={() =>
                                    setConfirmEnabled({ deployment: dep, enabled: !dep.enabled })
                                  }
                                  disabled={enabledMutation.isPending}
                                >
                                  {dep.enabled
                                    ? t('deployments.actions.disable', 'Disable')
                                    : t('deployments.actions.enable', 'Enable')}
                                </DropdownMenuItem>
                              ) : null}
                              {capabilities.has('manage_access') ? (
                                <DropdownMenuItem onClick={() => setShareTarget(dep)}>
                                  <Share2 className="h-4 w-4" aria-hidden="true" />
                                  {t('deployments.action.share', 'Share deployment')}
                                </DropdownMenuItem>
                              ) : null}
                              {capabilities.has('delete') ? (
                                <>
                                  <DropdownMenuSeparator />
                                  <DropdownMenuItem
                                    className="text-destructive focus:text-destructive"
                                    onClick={() => setConfirmDelete(dep)}
                                  >
                                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                                    {t('deployments.actions.delete', 'Delete')}
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
          <div className="shrink-0 border-t border-edge-subtle px-4 py-3">
            <WorkflowPagination
              page={page}
              pageCount={pageCount}
              totalItems={total}
              pageSize={PAGE_SIZE}
              onPageChange={(nextPage) => updateListParams({
                page: nextPage > 0 ? String(nextPage) : null,
              })}
            />
          </div>
        </section>
      </ManagementPageShell>

      <CreateDeploymentModal
        open={effectiveCreateOpen}
        onOpenChange={updateCreateOpen}
        initialWorkflowId={initialWorkflowId}
        initialName={initialDeploymentName}
        onCreated={() => {
          void qc.invalidateQueries({ queryKey: ['deployments'] });
        }}
      />

      <Dialog open={!!confirmDelete} onOpenChange={(open) => !open && setConfirmDelete(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('deployments.delete.title', 'Delete this deployment?')}</DialogTitle>
            <DialogDescription>
              {t(
                'deployments.delete.desc',
                'This disables and soft-deletes the deployment. The endpoint stops accepting traffic and the slug becomes reusable.',
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(null)} disabled={deleteMutation.isPending}>
              {t('deployments.delete.cancel', 'Cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={() => confirmDelete && deleteMutation.mutate(confirmDelete.id)}
              disabled={deleteMutation.isPending}
            >
              {t('deployments.delete.confirm', 'Delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={!!confirmEnabled} onOpenChange={(open) => !open && setConfirmEnabled(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {confirmEnabled?.enabled
                ? t('deployments.confirmEnable.title', 'Enable this deployment?')
                : t('deployments.confirmDisable.title', 'Disable this deployment?')}
            </DialogTitle>
            <DialogDescription>
              {confirmEnabled?.enabled
                ? t(
                    'deployments.confirmEnable.description',
                    '“{{name}}” will begin accepting authenticated requests.',
                    { name: confirmEnabled?.deployment.name },
                  )
                : t(
                    'deployments.confirmDisable.description',
                    '“{{name}}” will stop accepting new requests until it is enabled again.',
                    { name: confirmEnabled?.deployment.name },
                  )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setConfirmEnabled(null)}
              disabled={enabledMutation.isPending}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button
              variant={confirmEnabled?.enabled ? 'default' : 'destructive'}
              onClick={() => {
                if (!confirmEnabled) return;
                enabledMutation.mutate({
                  id: confirmEnabled.deployment.id,
                  enabled: confirmEnabled.enabled,
                });
              }}
              disabled={enabledMutation.isPending}
            >
              {confirmEnabled?.enabled
                ? t('deployments.actions.enable', 'Enable')
                : t('deployments.actions.disable', 'Disable')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {shareTarget ? (
        <ResourceShareDialog
          open
          onOpenChange={(open) => !open && setShareTarget(null)}
          resourceKind="deployment"
          resourceId={shareTarget.id}
          resourceName={shareTarget.name}
          effectiveRole={shareTarget.access?.effective_role}
          accessSource={shareTarget.access?.source}
        />
      ) : null}
    </>
  );
}
