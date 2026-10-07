import { ResourceSort } from '@/components/resources/ResourceSort';
import { useResourceSort, compareResourceValues } from '@/lib/resource-sort';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import {
  BadgeCheck,
  BookOpen,
  ChevronDown,
  Loader2,
  MoreHorizontal,
  Pencil,
  PlugZap,
  RefreshCw,
  Search,
  ServerCog,
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
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  useDeleteMcpServer,
  useMcpCatalog,
  useMcpServers,
  useRefreshMcpServer,
  useUpdateMcpServer,
} from '@/lib/api/queries/mcp-servers';
import type {
  McpCatalogItem,
  McpCatalogSource,
  McpServer,
} from '@/lib/api/mcp-servers';
import { McpServerFormDialog } from '@/pages/mcp-servers/McpServerFormDialog';
import { ManagementPageShell, ManagementToolbar } from '@/components/layout/management-page-shell';
import { StatusBadge, type SemanticStatus } from '@/components/ui/status';
import { ActionableError } from '@/components/presentation/ActionableError';
import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { AsyncState } from '@/components/ui/async-state';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';

type StatusFilter = 'all' | 'enabled' | 'failed';
type PageTab = 'installed' | 'discover';

function isHandshakeFailed(server: McpServer): boolean {
  return server.connection_status === 'connection_failed'
    || (server.last_handshake_status ?? '').startsWith('error');
}

function statusLabel(server: McpServer): { key: string; fallback: string; status: SemanticStatus } {
  if (server.connection_status === 'connection_required') {
    return { key: 'mcp.status.connection_required', fallback: 'Connection Required', status: 'warning' };
  }
  if (server.connection_status === 'connecting') {
    return { key: 'mcp.status.connecting', fallback: 'Connecting', status: 'running' };
  }
  if (server.connection_status === 'reconnect_required') {
    return { key: 'mcp.status.reconnect_required', fallback: 'Reconnect Required', status: 'warning' };
  }
  if (server.connection_status === 'connection_failed') {
    return { key: 'mcp.status.connection_failed', fallback: 'Connection Failed', status: 'danger' };
  }
  if (!server.enabled) return { key: 'mcp.status.disabled', fallback: 'Disabled', status: 'neutral' };
  if (server.last_handshake_status === 'ok') {
    return {
      key: 'mcp.status.active',
      fallback: 'Active',
      status: 'success',
    };
  }
  if (isHandshakeFailed(server)) {
    return { key: 'mcp.status.probe_failed', fallback: 'Probe Failed', status: 'danger' };
  }
  return {
    key: 'mcp.status.needs_probe',
    fallback: 'Needs Probe',
    status: 'warning',
  };
}

function McpServerCard({
  server,
  onEdit,
  onDelete,
}: {
  server: McpServer;
  onEdit: (server: McpServer) => void;
  onDelete: (server: McpServer) => void;
}) {
  const { t } = useTranslation();
  const updateMutation = useUpdateMcpServer();
  const refreshMutation = useRefreshMcpServer();
  const status = statusLabel(server);
  const statusText = t(status.key, status.fallback);
  const capabilities = new Set(server.access?.capabilities ?? []);

  return (
    <article
      data-testid="mcp-card"
      className="group flex flex-col gap-3 rounded-lg border border-edge-subtle bg-surface-work p-4 text-left transition-colors duration-feedback hover:border-edge-structural hover:bg-surface-hover/50 focus-within:border-focus"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
            <PlugZap className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <Link
              to={`/mcp-servers/${server.id}`}
              className="block truncate font-medium underline-offset-4 hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              {server.name}
            </Link>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
              <span className="max-w-full truncate rounded bg-secondary px-1.5 py-0.5 font-mono text-secondary-foreground">
                {server.tool_prefix}
              </span>
              <span>{server.transport}</span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {capabilities.has('update') ? <Switch
            checked={server.enabled}
            disabled={updateMutation.isPending || (server.auth_mode === 'oauth' && server.connection_status !== 'connected')}
            onCheckedChange={() =>
              updateMutation.mutate(
                { id: server.id, patch: { enabled: !server.enabled } },
                { onError: (error) => toast.error(error instanceof Error ? error.message : String(error)) },
              )
            }
            data-testid="mcp-card-toggle"
            aria-label={server.enabled
              ? t('mcp.disable', 'Disable')
              : t('mcp.enable', 'Enable')}
          /> : null}
          {capabilities.has('update') || capabilities.has('manage_secret') || capabilities.has('delete') ? <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                data-testid="mcp-card-menu"
                aria-label={t('mcp.open_menu', {
                  name: server.name,
                  defaultValue: 'Open actions for {{name}}',
                })}
              >
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {capabilities.has('update') || capabilities.has('manage_secret') ? (
                <DropdownMenuItem data-testid="mcp-menu-edit" onSelect={() => onEdit(server)}>
                  <Pencil />
                  {t('mcp.edit', 'Edit')}
                </DropdownMenuItem>
              ) : null}
              {capabilities.has('update') ? <DropdownMenuItem
                data-testid="mcp-menu-refresh"
                onSelect={() =>
                  refreshMutation.mutate(server.id, {
                    onSuccess: () => toast.success(t('mcp.refreshed', 'Connection Test Refreshed')),
                    onError: (error) => toast.error(error instanceof Error ? error.message : String(error)),
                  })
                }
                disabled={refreshMutation.isPending || (server.auth_mode === 'oauth' && server.connection_status !== 'connected')}
              >
                <RefreshCw />
                {t('mcp.refresh', 'Test Connection')}
              </DropdownMenuItem> : null}
              {capabilities.has('delete') ? <DropdownMenuItem
                data-testid="mcp-menu-delete"
                onSelect={() => onDelete(server)}
                className="text-destructive focus:bg-destructive/10 focus:text-destructive"
              >
                <Trash2 />
                {t('mcp.delete', 'Uninstall')}
              </DropdownMenuItem> : null}
            </DropdownMenuContent>
          </DropdownMenu> : null}
        </div>
      </div>
      <p className="[overflow-wrap:anywhere] line-clamp-2 min-h-10 text-sm text-muted-foreground">
        {server.description || t('mcp.no_description', 'No Brief Description Saved Yet.')}
      </p>
      <div className="truncate text-xs text-muted-foreground" title={server.endpoint}>
        {server.endpoint}
      </div>
      <ResourceProvenanceLine provenance={server.provenance} />
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={status.status}>
          {statusText}
        </StatusBadge>
        <span className="inline-flex items-center rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
          {t('mcp.tools_count', { count: server.last_tool_count ?? 0, defaultValue: '{{count}} Tools' })}
        </span>
      </div>
    </article>
  );
}

function CatalogCard({ item, installed }: { item: McpCatalogItem; installed?: McpServer }) {
  const { t } = useTranslation();
  const href = `/mcp-servers/discover/${item.source}?id=${encodeURIComponent(item.source_id)}`;
  const detailHref = installed ? `/mcp-servers/${installed.id}` : href;
  return (
    <article
      data-testid="mcp-catalog-card"
      className="group flex flex-col gap-3 rounded-lg border border-edge-subtle bg-surface-work p-4 transition-colors hover:border-edge-structural hover:bg-surface-hover/40 focus-within:border-focus"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="truncate font-medium">
              <Link to={detailHref} className="underline-offset-4 hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus">
                {item.name}
              </Link>
            </h3>
            {item.verified ? <BadgeCheck className="h-4 w-4 shrink-0 text-state-success" aria-label={t('mcp.verified', 'Verified')} /> : null}
          </div>
          <div className="mt-1 truncate font-mono text-xs text-muted-foreground">{item.source_id}</div>
        </div>
        {item.version ? <span className="shrink-0 text-xs text-muted-foreground">v{item.version}</span> : null}
      </div>
      <p className="[overflow-wrap:anywhere] line-clamp-3 min-h-[3.75rem] text-sm leading-5 text-muted-foreground">
        {item.description || t('mcp.catalog.no_description', 'No Description Provided.')}
      </p>
      <div className="mt-auto flex items-center justify-between gap-3 border-t pt-3">
        <div className="text-xs text-muted-foreground">
          {item.usage_count != null
            ? t('mcp.catalog.use_count', { count: item.usage_count, defaultValue: '{{count}} Uses' })
            : item.connection?.transport.replace('_', ' ') ?? t('mcp.catalog.setup_required', 'Setup Required')}
        </div>
        <Button size="sm" variant={installed ? 'outline' : 'default'} asChild>
          <Link to={detailHref}>
            {installed ? t('mcp.catalog.installed_button', 'Installed') : t('mcp.catalog.view_details', 'View Details')}
          </Link>
        </Button>
      </div>
    </article>
  );
}

export function McpServersPage() {
  const { t } = useTranslation();
  const sorting = useResourceSort(['updated_at', 'created_at', 'name'] as const, 'updated_at');
  const [urlParams, setUrlParams] = useSearchParams();
  const query = useMcpServers();
  const deleteMutation = useDeleteMcpServer();
  const tab: PageTab = urlParams.get('tab') === 'discover' ? 'discover' : 'installed';
  const source: McpCatalogSource = urlParams.get('source') === 'smithery' ? 'smithery' : 'official';
  const [formOpen, setFormOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<McpServer | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<McpServer | null>(null);
  const [search, setSearch] = useState('');
  const [discoverSearch, setDiscoverSearch] = useState('');
  const [submittedDiscoverSearch, setSubmittedDiscoverSearch] = useState('');
  const [lastTriggeredSearch, setLastTriggeredSearch] = useState<string | null>(null);
  const [discoverLimit, setDiscoverLimit] = useState(10);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const sourceMeta: Record<McpCatalogSource, { description: string; placeholder: string }> = {
    official: {
      description: t('mcp.source.official.description', 'Canonical publisher metadata from the vendor-neutral MCP Registry.'),
      placeholder: t('mcp.source.official.placeholder', 'e.g. github, filesystem, io.github.owner/server'),
    },
    smithery: {
      description: t('mcp.source.smithery.description', 'A community marketplace searchable by service, capability, or provider.'),
      placeholder: t('mcp.source.smithery.placeholder', 'e.g. Gmail, web search, Postgres'),
    },
  };

  const catalogQuery = useMcpCatalog(source, submittedDiscoverSearch, discoverLimit, { enabled: tab === 'discover' });
  const items = useMemo(() => query.data ?? [], [query.data]);
  const failedCount = items.filter(isHandshakeFailed).length;
  const filtered = useMemo(() => {
    const text = search.trim().toLowerCase();
    return items.filter((server) => {
      if (statusFilter === 'enabled' && !server.enabled) return false;
      if (statusFilter === 'failed' && !isHandshakeFailed(server)) return false;
      return !text || server.name.toLowerCase().includes(text) || server.endpoint.toLowerCase().includes(text) || (server.description ?? '').toLowerCase().includes(text);
    }).sort((left, right) => compareResourceValues(left[sorting.field], right[sorting.field], sorting.direction, sorting.field === 'name') || left.id.localeCompare(right.id));
  }, [items, search, statusFilter, sorting.field, sorting.direction]);
  const installedForCandidate = (candidate: McpCatalogItem) =>
    items.find(
      (server) =>
        server.name === candidate.name ||
        (!!candidate.connection && server.endpoint === candidate.connection.endpoint),
    );

  const changeTab = (next: string) => {
    const nextTab = next as PageTab;
    setUrlParams(nextTab === 'discover' ? { tab: 'discover', source } : {}, { replace: true });
  };
  const changeSource = (next: string) => {
    const nextSource = next as McpCatalogSource;
    setDiscoverSearch('');
    setSubmittedDiscoverSearch('');
    setLastTriggeredSearch(null);
    setDiscoverLimit(10);
    setUrlParams({ tab: 'discover', source: nextSource }, { replace: true });
  };
  const submitDiscoverSearch = () => {
    const nextSearch = discoverSearch.trim();
    setDiscoverLimit(10);
    setLastTriggeredSearch(nextSearch);
    if (nextSearch === submittedDiscoverSearch) {
      void catalogQuery.refetch();
      return;
    }
    setSubmittedDiscoverSearch(nextSearch);
  };
  const isSearchPending = lastTriggeredSearch !== null
    && lastTriggeredSearch === submittedDiscoverSearch
    && discoverLimit === 10
    && catalogQuery.isFetching;
  const handleDelete = async () => {
    if (!confirmDelete) return;
    try {
      await deleteMutation.mutateAsync(confirmDelete.id);
      toast.success(t('mcp.deleted', 'MCP Server Uninstalled'));
      setConfirmDelete(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };

  const catalogHeading = source === 'smithery'
    ? t('mcp.catalog.popular', 'Popular On Smithery')
    : t('mcp.catalog.browse_official', 'Browse Official Registry');

  return (
    <>
      <ManagementPageShell
        resourceKind="mcp"
        title={t('mcp.title', 'MCP Servers')}
        description={t('mcp.subtitle', 'Discover and manage external tools that agents can load when needed.')}
        icon={PlugZap}
        actions={<Button
            variant="outline"
            onClick={() => {
              setEditTarget(null);
              setFormOpen(true);
            }}
            data-testid="mcp-add-button"
          >
            <ServerCog className="h-4 w-4" />
            {t('mcp.add', 'Add Custom Server')}
          </Button>}
      >

        <Tabs value={tab} onValueChange={changeTab} className="flex min-h-0 flex-1 flex-col gap-4">
          <TabsList variant="underline" className="w-fit shrink-0">
            <TabsTrigger value="installed">
              {t('mcp.tab.installed', 'Installed')}
              <span className="ml-1.5 rounded bg-background/80 px-1.5 py-0.5 text-xs tabular-nums">{items.length}</span>
            </TabsTrigger>
            <TabsTrigger value="discover">{t('mcp.tab.discover', 'Discover')}</TabsTrigger>
          </TabsList>

          <TabsContent value="installed" className="mt-0 flex min-h-0 flex-1 flex-col gap-4 overflow-hidden data-[state=inactive]:hidden">
            <ManagementToolbar className="flex-col items-stretch">
              <div className="relative min-w-[16rem] flex-1 max-w-md">
                <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                <Input data-testid="mcp-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('mcp.search_ph', 'Search Installed Servers')} className="pl-9" />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as StatusFilter)}>
                  <SelectTrigger data-testid="mcp-filter" className="w-44" aria-label={t('mcp.filter_status', 'Filter by server status')}><SelectValue /></SelectTrigger>
                  <SelectContent className="max-h-72">
                    <SelectItem value="all">{t('mcp.filter_all', 'All Statuses')}</SelectItem>
                    <SelectItem value="enabled">{t('mcp.filter_enabled', 'Enabled')}</SelectItem>
                    <SelectItem value="failed">{t('mcp.filter_failed', 'Probe Failed')}</SelectItem>
                  </SelectContent>
                </Select>
                <ResourceSort fields={['updated_at', 'created_at', 'name']} value={sorting.value} onValueChange={sorting.setValue} />
                {failedCount > 0 ? (
                  <button type="button" onClick={() => setStatusFilter('failed')} className="text-xs font-medium text-destructive hover:underline">
                    {t('mcp.needs_attention', { count: failedCount, defaultValue: '{{count}} Need Attention' })}
                  </button>
                ) : null}
              </div>
            </ManagementToolbar>

            <div className="page-scroll-region flex-1 pr-1">
              <section className="space-y-3" aria-labelledby="custom-mcp-heading">
                <div>
                  <h2 id="custom-mcp-heading" className="text-sm font-semibold">
                    {t('mcp.custom.heading', 'Custom')}
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {t('mcp.custom.hint', 'User-managed MCP connections loaded inside the selected Agent Runtime.')}
                  </p>
                </div>
            {query.isLoading ? <AsyncState kind="loading" title={t('mcp.loading', 'Loading…')} /> : query.isError ? (
              <ActionableError
                title={t('mcp.load_error', 'Failed To Load MCP Servers.')}
                description={t('mcp.load_error_hint', 'Check your connection, then reload the installed servers.')}
                actionLabel={t('retry', 'Retry')}
                onAction={() => void query.refetch()}
                technicalDetails={query.error instanceof Error ? query.error.message : String(query.error)}
                technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
              />
            ) : items.length === 0 ? (
              <CompactEmptyState
                data-testid="mcp-empty-state"
                title={t('mcp.empty', 'No MCP Servers Installed Yet.')}
                description={t('mcp.emptyHint', 'Open Discover to find a server, or add a custom server by URL or command.')}
                actionLabel={t('mcp.open_discover', 'Open Discover')}
                onAction={() => changeTab('discover')}
              />
            ) : filtered.length === 0 ? (
              <div className="empty-state" data-testid="mcp-no-match">
                <div className="empty-state-title">{t('mcp.no_match', 'No Servers Match Your Search.')}</div>
                <div className="empty-state-copy">{t('mcp.noMatchHint', 'Adjust the search text or status filter.')}</div>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {filtered.map((server) => (
                  <McpServerCard key={server.id} server={server} onEdit={(target) => { setEditTarget(target); setFormOpen(true); }} onDelete={setConfirmDelete} />
                ))}
              </div>
            )}
              </section>
            </div>
          </TabsContent>

          <TabsContent value="discover" className="mt-0 flex min-h-0 flex-1 flex-col gap-4 overflow-hidden data-[state=inactive]:hidden">
            <ManagementToolbar className="flex-col items-stretch">
              <form
                className="flex flex-wrap items-center gap-3"
                onSubmit={(event) => {
                  event.preventDefault();
                  submitDiscoverSearch();
                }}
              >

                <div className="relative min-w-[16rem] flex-1">
                  <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                  <Input
                    value={discoverSearch}
                    onChange={(event) => {
                      setDiscoverSearch(event.target.value);
                    }}
                    placeholder={sourceMeta[source].placeholder}
                    className="pl-9"
                    data-testid="mcp-catalog-search"
                  />
                </div>
                <Button
                  type="submit"
                  className="min-w-28"
                  disabled={isSearchPending}
                  data-testid="mcp-catalog-search-button"
                >
                  {isSearchPending ? (
                    <>
                      <Loader2 className="animate-spin" />
                      {t('mcp.catalog.searching', 'Searching…')}
                    </>
                  ) : (
                    <>
                      <Search />
                      {t('mcp.catalog.search', 'Search')}
                    </>
                  )}
                </Button>
              </form>
              <div className="flex flex-wrap items-center gap-2">
                <Select value={source} onValueChange={changeSource}>
                  <SelectTrigger className="w-56" aria-label={t('mcp.catalog.source', 'Select MCP catalog')}><SelectValue /></SelectTrigger>
                  <SelectContent className="max-h-72">
                    <SelectItem value="official">{t('mcp.source.official.name', 'Official MCP Registry')}</SelectItem>
                    <SelectItem value="smithery">{t('mcp.source.smithery.name', 'Smithery')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">{sourceMeta[source].description}</p>
              <div className="mt-3 rounded-lg border border-edge-subtle bg-surface-work p-4">
                <div className="flex items-start gap-3">
                  <BookOpen className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                  <div>
                    <div className="font-medium">{t('mcp.install_guideline.title', 'Install Guideline')}</div>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {t('mcp.install_guideline.body', 'Open a server to review its publisher, access, and required credentials. The connection is tested before installation. Use Add Custom Server only for private or unlisted servers.')}
                    </p>
                  </div>
                </div>
              </div>
            </ManagementToolbar>

            {!submittedDiscoverSearch ? <div className="flex shrink-0 items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold">{catalogHeading}</h2>
                {source === 'official' ? (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {t('mcp.catalog.no_official_ranking', 'The official registry does not publish a popularity ranking.')}
                  </p>
                ) : null}
              </div>
            </div> : null}

            <div className="page-scroll-region flex-1 pr-1">
            {catalogQuery.isLoading ? <AsyncState kind="loading" title={t('mcp.catalog.loading', 'Loading Catalog…')} /> : catalogQuery.isError ? (
              <ActionableError
                title={t('mcp.catalog.load_error', 'This MCP catalog is temporarily unavailable.')}
                description={t('mcp.catalog.load_error_hint', 'Check the selected registry and try loading the catalog again.')}
                actionLabel={t('retry', 'Retry')}
                onAction={() => void catalogQuery.refetch()}
                technicalDetails={catalogQuery.error instanceof Error ? catalogQuery.error.message : String(catalogQuery.error)}
                technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
              />
            ) : (catalogQuery.data?.items.length ?? 0) === 0 ? (
              <div className="empty-state">
                <div className="empty-state-title">{t('mcp.catalog.no_results', 'No MCP Servers Found.')}</div>
                <div className="empty-state-copy">{t('mcp.catalog.no_results_hint', 'Try a product name, capability, or publisher namespace.')}</div>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
                {catalogQuery.data?.items.map((candidate) => (
                  <CatalogCard key={`${candidate.source}:${candidate.source_id}`} item={candidate} installed={installedForCandidate(candidate)} />
                ))}
              </div>
            )}

            {catalogQuery.data?.has_more ? (
              <div className="flex justify-center py-5">
                <Button
                  type="button"
                  variant="outline"
                  data-testid="mcp-catalog-more"
                  disabled={catalogQuery.isPlaceholderData || catalogQuery.isFetching}
                  onClick={() => setDiscoverLimit((current) => current + 10)}
                >
                  {catalogQuery.isPlaceholderData || catalogQuery.isFetching
                    ? <Loader2 className="animate-spin" />
                    : <ChevronDown />}
                  {t('mcp.catalog.more', 'More')}
                </Button>
              </div>
            ) : null}
            </div>
          </TabsContent>
        </Tabs>
      </ManagementPageShell>

      {formOpen ? <McpServerFormDialog open={formOpen} onOpenChange={setFormOpen} target={editTarget} /> : null}
      <Dialog open={!!confirmDelete} onOpenChange={(open) => !open && setConfirmDelete(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('mcp.delete_title', 'Uninstall This MCP Server?')}</DialogTitle>
            <DialogDescription>{t('mcp.delete_confirm', 'This removes the server and its stored credentials. Agents will no longer be able to load it.')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(null)}>{t('mcp.cancel', 'Cancel')}</Button>
            <Button variant="destructive" data-testid="mcp-confirm-delete" onClick={() => void handleDelete()} disabled={deleteMutation.isPending}>{t('mcp.delete', 'Uninstall')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
