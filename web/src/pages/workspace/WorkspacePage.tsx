/**
 * `/workspace` — the workflow landing page.
 *
 * Layout: a header bar ("Workflows" + "New workflow"), a toolbar with a
 * client-side name search, then a paginated TABLE of workflows (Linear/Notion
 * style). Each row shows name, latest version, updated time, and per-row
 * actions (Open / Duplicate / kebab → Edit info / Delete). Column headers for
 * Name and Updated are clickable to toggle sort direction.
 *
 * Filtering / sorting / pagination are all client-side: the list endpoint is
 * fetched once with a generous limit (it pages on the server too, but for the
 * scale of a single user's workspace a client window is simpler and lets us
 * search across the whole set without round-trips). If a workspace ever grows
 * past `FETCH_LIMIT`, server pagination should be reintroduced.
 *
 * Dialog state is owned here (single instance per dialog type, target workflow
 * tracked via state) so rows stay render-only. Duplicate opens a dialog that
 * lets the user name + describe the copy before the frontend-only compose of
 * GET-snapshot → create → commit runs (see `useDuplicateWorkflow`).
 */
import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { Plus, Search, ArrowUp, ArrowDown, ChevronsUpDown, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  useWorkspaceCatalog,
  useWorkspaceList,
  workspaceListQueryOptions,
} from '@/lib/api/queries/workflows';
import {
  useWorkflowSandboxStatuses,
  type WorkflowSandboxStatus,
} from '@/lib/api/queries/workflow-sandbox';
import type { components } from '@/lib/api/schema';
import { WorkflowRow } from '@/pages/workspace/WorkflowRow';
import { filterWorkflows } from '@/pages/workspace/filterWorkflows';
import { CreateWorkflowDialog } from '@/pages/workspace/CreateWorkflowDialog';
import { EditWorkflowInfoDialog } from '@/pages/workspace/EditWorkflowInfoDialog';
import { DeleteWorkflowDialog } from '@/pages/workspace/DeleteWorkflowDialog';
import { DuplicateWorkflowDialog } from '@/pages/workspace/DuplicateWorkflowDialog';
import { WorkflowPagination } from '@/pages/workspace/WorkflowPagination';
import { ManagementPageShell, ManagementToolbar } from '@/components/layout/management-page-shell';
import { ActionableError } from '@/components/presentation/ActionableError';
import { SharedResourceList } from '@/components/resources/SharedResourceList';
import {
  ResourceScopeSwitch,
  type ResourceListScope,
} from '@/components/resources/ResourceScopeSwitch';

type WorkflowMetaOut = components['schemas']['WorkflowMetaOut'];

const FETCH_LIMIT = 200;
const PAGE_SIZE = 15;

type SortKey = 'name' | 'updated';
type SortDir = 'asc' | 'desc';
type SortOption = 'updated_desc' | 'updated_asc' | 'name_asc' | 'name_desc';
type SandboxFilter = 'all' | 'running' | 'hibernated' | 'idle' | 'closed';

function readSortOption(value: SortOption): { sortKey: SortKey; sortDir: SortDir } {
  switch (value) {
    case 'updated_asc':
      return { sortKey: 'updated', sortDir: 'asc' };
    case 'name_asc':
      return { sortKey: 'name', sortDir: 'asc' };
    case 'name_desc':
      return { sortKey: 'name', sortDir: 'desc' };
    case 'updated_desc':
    default:
      return { sortKey: 'updated', sortDir: 'desc' };
  }
}

function writeSortOption(sortKey: SortKey, sortDir: SortDir): SortOption {
  if (sortKey === 'name') return sortDir === 'asc' ? 'name_asc' : 'name_desc';
  return sortDir === 'asc' ? 'updated_asc' : 'updated_desc';
}

export function WorkspacePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<WorkflowMetaOut | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<WorkflowMetaOut | null>(
    null,
  );
  const [duplicateTarget, setDuplicateTarget] =
    useState<WorkflowMetaOut | null>(null);

  const search = searchParams.get('q') ?? '';
  const resourceScope: ResourceListScope = searchParams.get('scope') === 'shared'
    ? 'shared'
    : 'owned';
  const sortValue = searchParams.get('sort');
  const sortOption: SortOption = sortValue === 'updated_asc' || sortValue === 'name_asc' || sortValue === 'name_desc'
    ? sortValue
    : 'updated_desc';
  const { sortKey, sortDir } = readSortOption(sortOption);
  const sandboxValue = searchParams.get('sandbox');
  const sandboxFilter: SandboxFilter = sandboxValue === 'running' || sandboxValue === 'hibernated' || sandboxValue === 'idle' || sandboxValue === 'closed'
    ? sandboxValue
    : 'all';
  const rawPage = Number.parseInt(searchParams.get('page') ?? '0', 10);
  const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 0;
  const needsCatalog = search.trim().length > 0
    || sandboxFilter !== 'all'
    || sortOption !== 'updated_desc';
  const pageWorkspace = useWorkspaceList(
    PAGE_SIZE,
    page * PAGE_SIZE,
    resourceScope === 'owned' && !needsCatalog,
  );
  const catalogWorkspace = useWorkspaceCatalog(
    resourceScope === 'owned' && needsCatalog,
    FETCH_LIMIT,
  );
  const workspace = needsCatalog ? catalogWorkspace : pageWorkspace;
  const updateListParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(updates)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setSearchParams(next, { replace: true });
  };
  const setSearch = (value: string) => updateListParams({ q: value || null, page: null });
  const setSandboxFilter = (value: SandboxFilter) => updateListParams({
    sandbox: value === 'all' ? null : value,
    page: null,
  });
  const setSort = (key: SortKey, direction: SortDir) => updateListParams({
    sort: writeSortOption(key, direction) === 'updated_desc' ? null : writeSortOption(key, direction),
    page: null,
  });
  const setPage = (value: number) => updateListParams({ page: value > 0 ? String(value) : null });
  const setResourceScope = (value: ResourceListScope) => updateListParams({
    scope: value === 'shared' ? 'shared' : null,
    page: null,
    sandbox: null,
  });

  const items = useMemo(
    () => workspace.data?.items ?? [],
    [workspace.data?.items],
  );
  const isLoading = workspace.isLoading;
  const isError = workspace.isError;

  const textMatchedAndSorted = useMemo(() => {
    const base = filterWorkflows(items, search);
    return [...base].sort((a, b) => {
      let cmp: number;
      if (sortKey === 'name') {
        cmp = (a.workflow_name || a.wf_id).localeCompare(
          b.workflow_name || b.wf_id,
        );
      } else {
        cmp = (a.updated_at ?? 0) - (b.updated_at ?? 0);
      }
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [items, search, sortDir, sortKey]);
  const sandboxStatusIds = useMemo(() => {
    if (sandboxFilter !== 'all') {
      return textMatchedAndSorted.map((wf) => wf.wf_id);
    }
    if (!needsCatalog) return items.map((wf) => wf.wf_id);
    return textMatchedAndSorted
      .slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE)
      .map((wf) => wf.wf_id);
  }, [items, needsCatalog, page, sandboxFilter, textMatchedAndSorted]);
  const sandboxStatuses = useWorkflowSandboxStatuses(
    sandboxStatusIds,
    resourceScope === 'owned' && sandboxStatusIds.length > 0,
  );
  const sandboxByWorkflowId = useMemo(() => {
    const map = new Map<string, WorkflowSandboxStatus>();
    for (const item of sandboxStatuses.data?.items ?? []) {
      const id = item.wf_id ?? item.scope_id;
      map.set(id, item);
    }
    return map;
  }, [sandboxStatuses.data?.items]);

  // Cross-page filtering/sorting activates the catalog query. The default
  // newest-first view remains server paginated and skips this full-list work.
  const filtered = useMemo(() => {
    return sandboxFilter === 'all'
      ? textMatchedAndSorted
      : textMatchedAndSorted.filter((wf) => {
          const status = sandboxByWorkflowId.get(wf.wf_id)?.status ?? 'idle';
          if (sandboxFilter === 'running') {
            return [
              'running',
              'hibernating',
              'restoring',
              'releasing',
            ].includes(status);
          }
          return status === sandboxFilter;
        });
  }, [sandboxByWorkflowId, sandboxFilter, textMatchedAndSorted]);

  const totalItems = needsCatalog
    ? filtered.length
    : pageWorkspace.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  // Clamp the page in case filtering shrank the result set below the cursor.
  const safePage = Math.min(page, pageCount - 1);
  const pageItems = needsCatalog
    ? filtered.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE)
    : items;

  useEffect(() => {
    if (needsCatalog || !pageWorkspace.data) return;
    const nextOffset = (page + 1) * PAGE_SIZE;
    if (nextOffset >= pageWorkspace.data.total) return;
    void queryClient.prefetchQuery(workspaceListQueryOptions(PAGE_SIZE, nextOffset));
  }, [needsCatalog, page, pageWorkspace.data, queryClient]);
  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSort(key, sortDir === 'asc' ? 'desc' : 'asc');
    } else {
      setSort(key, key === 'name' ? 'asc' : 'desc');
    }
  };

  const filtersActive = search.trim().length > 0 || sandboxFilter !== 'all';
  const clearFilters = () => {
    updateListParams({ q: null, sandbox: null, page: null });
  };

  const sortIcon = (key: SortKey) => {
    if (sortKey !== key) return <ChevronsUpDown className="size-3.5 opacity-50" />;
    return sortDir === 'asc' ? (
      <ArrowUp className="size-3.5" />
    ) : (
      <ArrowDown className="size-3.5" />
    );
  };

  if (resourceScope === 'shared') {
    return (
      <ManagementPageShell
        resourceKind="workflow"
        title={t('workspace_header', 'Workflows')}
        className="gap-6"
      >
        <ResourceScopeSwitch value={resourceScope} onValueChange={setResourceScope} />
        <ManagementToolbar className="rounded-lg border-x border-edge-subtle">
          <div className="relative min-w-[220px] flex-1 sm:max-w-sm">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="pl-8"
              placeholder={t('workspace.searchShared', 'Search shared workflows…')}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
        </ManagementToolbar>
        <SharedResourceList resourceType="workflow" search={search} />
      </ManagementPageShell>
    );
  }

  return (
    <>
      <ManagementPageShell
        resourceKind="workflow"
        title={t('workspace_header', 'Workflows')}
        actions={<Button onClick={() => setCreateOpen(true)}>
            <Plus />
            {t('new_workflow', 'New Workflow')}
          </Button>}
        className="gap-6"
      >

        <ResourceScopeSwitch value={resourceScope} onValueChange={setResourceScope} />

        {isLoading ? (
          <div className="flex flex-col gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-12 rounded-lg" />
            ))}
          </div>
        ) : isError ? (
          <ActionableError
            title={t('workspace_load_error', 'Failed to load workflows.')}
            description={t('workspace_load_error_hint', 'Check the connection and load the workflow list again.')}
            actionLabel={t('retry', 'Retry')}
            onAction={() => void workspace.refetch()}
            technicalDetails={workspace.error instanceof Error ? workspace.error.message : String(workspace.error)}
            technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
          />
        ) : (
          // Bounded column: toolbar + pagination stay pinned; only the table
          // region (flex-1) scrolls when the viewport is short.
          <div className="flex min-h-0 flex-1 flex-col gap-4">
            <ManagementToolbar className="rounded-lg border-x border-edge-subtle">
              <div className="relative min-w-[220px] flex-1 sm:max-w-sm">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  data-testid="wf-search"
                  className="pl-8"
                  aria-label={t('search_workflows', 'Search by name, ID, or description…')}
                  placeholder={t(
                    'search_workflows',
                    'Search by name, ID, or description…',
                  )}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              <Select
                value={sandboxFilter}
                onValueChange={(value) => {
                  setSandboxFilter(value as SandboxFilter);
                }}
              >
                <SelectTrigger className="w-[154px]" aria-label={t('workspace.filter_sandbox', 'Filter by sandbox')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('workspace.sandbox_all', 'All sandboxes')}</SelectItem>
                  <SelectItem value="running">{t('workspace.sandbox_running', 'Running')}</SelectItem>
                  <SelectItem value="hibernated">{t('workflow.sandbox.hibernated', 'Sandbox hibernated')}</SelectItem>
                  <SelectItem value="idle">{t('workflow.sandbox.idle', 'Sandbox idle')}</SelectItem>
                  <SelectItem value="closed">{t('workflow.sandbox.closed', 'Sandbox closed')}</SelectItem>
                </SelectContent>
              </Select>
              <Select
                value={sortOption}
                onValueChange={(value) => {
                  const next = readSortOption(value as SortOption);
                  setSort(next.sortKey, next.sortDir);
                }}
              >
                <SelectTrigger className="w-[160px]" aria-label={t('workspace.sort', 'Sort workflows')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="updated_desc">{t('workspace.sort_updated_desc', 'Updated newest')}</SelectItem>
                  <SelectItem value="updated_asc">{t('workspace.sort_updated_asc', 'Updated oldest')}</SelectItem>
                  <SelectItem value="name_asc">{t('workspace.sort_name_asc', 'Name A-Z')}</SelectItem>
                  <SelectItem value="name_desc">{t('workspace.sort_name_desc', 'Name Z-A')}</SelectItem>
                </SelectContent>
              </Select>
              {filtersActive && (
                <Button variant="ghost" size="sm" onClick={clearFilters}>
                  <X className="h-4 w-4" />
                  {t('clear', 'Clear')}
                </Button>
              )}
              <span className="ml-auto whitespace-nowrap text-meta">
                {filtersActive
                  ? t('wf_count_filtered', '{{shown}} of {{total}} workflows', {
                      shown: totalItems,
                      total: catalogWorkspace.data?.total ?? totalItems,
                    })
                  : t('wf_count', '{{n}} workflows', { n: totalItems })}
              </span>
            </ManagementToolbar>

            {/* Table — the scroll region. Capped to the available height so the
                pagination bar below never scrolls away with the rows. The
                percentage columns adapt to the available width; low-priority
                metadata disappears before the workflow name becomes cramped. */}
            <div
              className="surface-panel app-scrollbar min-h-0 flex-1 overflow-auto rounded-lg"
              data-testid="wf-table-scroll"
            >
              <table
                className="w-full table-fixed text-left text-ui"
                data-testid="wf-table"
              >
                <colgroup>
                  <col className="w-[50%] xl:w-[38%]" />
                  <col className="w-[12%] xl:w-[10%]" />
                  <col className="w-[20%] xl:w-[15%]" />
                  <col className="hidden w-[18%] xl:table-column" />
                  <col className="w-[18%] xl:w-[19%]" />
                </colgroup>
                <thead className="sticky top-0 z-10 border-b bg-muted/70 text-xs font-medium text-muted-foreground">
                  <tr>
                    <th className="py-2.5 pl-4 pr-3 font-medium">
                      <button
                        type="button"
                        data-testid="wf-sort-name"
                        className="inline-flex min-h-6 items-center gap-1 hover:text-foreground"
                        onClick={() => toggleSort('name')}
                      >
                        {t('col_name', 'Name')}
                        {sortIcon('name')}
                      </button>
                    </th>
                    <th className="px-3 py-2.5 font-medium">
                      {t('col_version', 'Version')}
                    </th>
                    <th className="px-3 py-2.5 font-medium">
                      {t('workspace.col_sandbox', 'Sandbox')}
                    </th>
                    <th className="hidden px-3 py-2.5 font-medium xl:table-cell">
                      <button
                        type="button"
                        data-testid="wf-sort-updated"
                        className="inline-flex min-h-6 items-center gap-1 hover:text-foreground"
                        onClick={() => toggleSort('updated')}
                      >
                        {t('col_updated', 'Updated')}
                        {sortIcon('updated')}
                      </button>
                    </th>
                    <th className="py-2.5 pl-3 pr-4 text-right font-medium">
                      {t('col_actions', 'Actions')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {pageItems.length === 0 ? (
                    <tr>
                      <td
                        colSpan={5}
                        className="px-4 py-10 text-center text-muted-foreground"
                        data-testid={filtersActive ? 'wf-no-match' : 'wf-empty'}
                      >
                        {filtersActive
                          ? t('no_match', 'No results found')
                          : t('no_workflow', 'No workflows yet. Click "New Workflow" to create one.')}
                      </td>
                    </tr>
                  ) : (
                    pageItems.map((wf) => (
                      <WorkflowRow
                        key={wf.wf_id}
                        wf={wf}
                        onEdit={setEditTarget}
                        onDelete={setDeleteTarget}
                        onDuplicate={setDuplicateTarget}
                        duplicating={false}
                        sandboxStatus={sandboxByWorkflowId.get(wf.wf_id)}
                      />
                    ))
                  )}
                </tbody>
              </table>
            </div>

            {/* Pagination — pinned below the scroll region. */}
            <div className="shrink-0">
              <WorkflowPagination
                page={safePage}
                pageCount={pageCount}
                totalItems={totalItems}
                pageSize={PAGE_SIZE}
                onPageChange={setPage}
              />
            </div>
          </div>
        )}
      </ManagementPageShell>

      <CreateWorkflowDialog open={createOpen} onOpenChange={setCreateOpen} />
      <EditWorkflowInfoDialog
        open={editTarget !== null}
        onOpenChange={(o) => {
          if (!o) setEditTarget(null);
        }}
        wf={editTarget}
      />
      <DeleteWorkflowDialog
        open={deleteTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDeleteTarget(null);
        }}
        wf={deleteTarget}
      />
      <DuplicateWorkflowDialog
        open={duplicateTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDuplicateTarget(null);
        }}
        wf={duplicateTarget}
      />
    </>
  );
}
