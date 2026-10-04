import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import { BadgeCheck, BookOpenText, ChevronDown, FileArchive, Loader2, MoreHorizontal, Pencil, Plus, Search, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useSetSkillInstalled, useDeleteSkill, useSaveCustomSkill, useSkillCatalog, useSkills } from '@/lib/api/queries/skills';
import type { Skill, SkillCatalogItem, SkillCatalogSource } from '@/lib/api/skills';
import { ManagementPageShell, ManagementToolbar } from '@/components/layout/management-page-shell';
import { ActionableError } from '@/components/presentation/ActionableError';
import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { AsyncState } from '@/components/ui/async-state';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import { ResourceAccessBadge } from '@/components/resources/ResourceAccessBadge';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';

type PageTab = 'all' | 'discover';
type InstalledSource = 'all' | SkillCatalogSource | 'custom';

function sourceLabel(source: string | null | undefined, t: (key: string, fallback: string) => string) {
  if (source === 'openai') return t('skills.source.openai', 'OpenAI Curated');
  if (source === 'anthropic') return t('skills.source.anthropic', 'Anthropic Public');
  if (source === 'custom') return t('skills.source.custom', 'Custom');
  return t('skills.source.unknown', 'Imported');
}

function SkillCard({ skill, onDelete }: { skill: Skill; onDelete: (skill: Skill) => void }) {
  const { t } = useTranslation();
  const capabilities = new Set(skill.access?.capabilities ?? []);
  const installation = useSetSkillInstalled();
  const toggleInstalled = async () => {
    try { await installation.mutateAsync({ id: skill.id, installed: !skill.installed }); }
    catch (error) { toast.error(error instanceof Error ? error.message : String(error)); }
  };
  return (
    <article
      data-testid="skill-card"
      className="group flex flex-col gap-3 rounded-lg border border-edge-subtle bg-surface-work p-4 text-left transition-colors hover:border-edge-structural hover:bg-surface-hover/40 focus-within:border-focus"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <ResourceIcon kind="skill" size="sm" />
          <div className="min-w-0">
            <h3 className="truncate font-medium">
              <Link
                to={`/skills/${skill.id}`}
                className="underline-offset-4 hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              >
                {skill.name}
              </Link>
            </h3>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
              <span>{sourceLabel(skill.source, t)}</span>
              <span>v{skill.version}</span>
            </div>
          </div>
        </div>
        {(capabilities.has('delete') || (skill.source === 'custom' && capabilities.has('update'))) && <div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                data-testid="skill-card-menu"
                aria-label={t('skills.open_menu', {
                  name: skill.name,
                  defaultValue: 'Open actions for {{name}}',
                })}
              >
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {skill.source === 'custom' && capabilities.has('update') ? (
                <DropdownMenuItem asChild>
                  <Link to={`/skills/${skill.id}?tab=instructions&edit=1`}>
                    <Pencil />
                    {t('skills.edit', 'Edit')}
                  </Link>
                </DropdownMenuItem>
              ) : null}
              {capabilities.has('delete') ? (
                <DropdownMenuItem
                  data-testid="skill-menu-delete"
                  onSelect={() => onDelete(skill)}
                  className="text-destructive focus:bg-destructive/10 focus:text-destructive"
                >
                  <Trash2 />
                  {t('skills.deleteResource', 'Delete Skill')}
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>}
      </div>
      <p className="[overflow-wrap:anywhere] line-clamp-3 min-h-[3.75rem] text-sm leading-5 text-muted-foreground">
        {skill.description || t('skills.no_description', 'No Description')}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <ResourceAccessBadge access={skill.access} />
        <ResourceProvenanceLine provenance={skill.provenance} />
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">{skill.installed ? t('skills.installState.installed', 'Installed') : t('skills.installState.uninstalled', 'Not installed')}</span>
        {capabilities.has('use') && <Button size="sm" variant="outline" disabled={installation.isPending} onClick={() => void toggleInstalled()}>
          {skill.installed ? t('skills.uninstall', 'Uninstall') : t('skills.install', 'Install')}
        </Button>}
      </div>
      <div className="mt-auto flex flex-wrap gap-1 border-t pt-3">
        {skill.allowed_tools.slice(0, 4).map((tool) => (
          <span key={tool} className="max-w-full truncate rounded bg-secondary px-1.5 py-0.5 font-mono text-xs text-secondary-foreground">{tool}</span>
        ))}
        {skill.allowed_tools.length > 4 ? <span className="text-xs text-muted-foreground">+{skill.allowed_tools.length - 4}</span> : null}
        {skill.allowed_tools.length === 0 ? <span className="text-xs text-muted-foreground">{t('skills.no_tools_short', 'No tool requirements')}</span> : null}
      </div>
    </article>
  );
}

function CatalogCard({ item, installed }: { item: SkillCatalogItem; installed?: Skill }) {
  const { t } = useTranslation();
  const href = `/skills/discover/${item.source}?id=${encodeURIComponent(item.source_id)}`;
  const detailHref = installed ? `/skills/${installed.id}` : href;
  return (
    <article
      data-testid="skill-catalog-card"
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
            <BadgeCheck className="h-4 w-4 shrink-0 text-state-success" aria-label={t('skills.verified', 'Verified Source')} />
          </div>
          <div className="mt-1 truncate font-mono text-xs text-muted-foreground">{item.source_id}</div>
        </div>
        <span className="shrink-0 text-xs text-muted-foreground">v{item.version}</span>
      </div>
      <p className="[overflow-wrap:anywhere] line-clamp-3 min-h-[3.75rem] text-sm leading-5 text-muted-foreground">{item.description}</p>
      <div className="mt-auto flex items-center justify-between gap-3 border-t pt-3">
        <span className="text-xs text-muted-foreground">
          {t('skills.files_count', { count: item.files.length, defaultValue: '{{count}} Files' })}
        </span>
        <Button size="sm" variant={installed ? 'outline' : 'default'} asChild>
          <Link to={detailHref}>
            {installed?.installed ? t('skills.installed_button', 'Installed') : t('skills.view_details', 'View Details')}
          </Link>
        </Button>
      </div>
    </article>
  );
}

export function SkillsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const skillsQuery = useSkills();
  const deleteMutation = useDeleteSkill();
  const saveCustomMutation = useSaveCustomSkill();
  const tab: PageTab = params.get('tab') === 'discover' ? 'discover' : 'all';
  const source: SkillCatalogSource = params.get('source') === 'anthropic' ? 'anthropic' : 'openai';
  const [installedSource, setInstalledSource] = useState<InstalledSource>('all');
  const [relationship, setRelationship] = useState('all');
  const [installationState, setInstallationState] = useState('all');
  const [search, setSearch] = useState('');
  const [discoverSearch, setDiscoverSearch] = useState('');
  const [submittedDiscoverSearch, setSubmittedDiscoverSearch] = useState('');
  const [lastTriggeredSearch, setLastTriggeredSearch] = useState<string | null>(null);
  const [discoverLimit, setDiscoverLimit] = useState(10);
  const [confirmDelete, setConfirmDelete] = useState<Skill | null>(null);
  const [customDialogOpen, setCustomDialogOpen] = useState(false);
  const [customBundle, setCustomBundle] = useState<File | null>(null);

  const catalogQuery = useSkillCatalog(source, submittedDiscoverSearch, discoverLimit, { enabled: tab === 'discover' });
  const items = useMemo(() => skillsQuery.data ?? [], [skillsQuery.data]);
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    return items.filter((skill) => {
      if (relationship === 'created' && !skill.created_by_me) return false;
      if (relationship === 'shared' && skill.created_by_me) return false;
      if (installationState === 'installed' && !skill.installed) return false;
      if (installationState === 'uninstalled' && skill.installed) return false;
      if (installedSource !== 'all' && skill.source !== installedSource) return false;
      return !query || skill.name.toLowerCase().includes(query) || skill.description.toLowerCase().includes(query) || skill.allowed_tools.some((tool) => tool.toLowerCase().includes(query));
    });
  }, [installedSource, items, search, relationship, installationState]);

  const installedFor = (item: SkillCatalogItem) =>
    items.find((skill) => skill.source === item.source && skill.source_id === item.source_id);
  const changeTab = (next: string) => {
    const value = next as PageTab;
    setParams(
      value === 'discover'
        ? { tab: 'discover', source }
        : {},
      { replace: true },
    );
  };
  const changeSource = (next: string) => {
    const value = next as SkillCatalogSource;
    setDiscoverSearch('');
    setSubmittedDiscoverSearch('');
    setLastTriggeredSearch(null);
    setDiscoverLimit(10);
    setParams({ tab: 'discover', source: value }, { replace: true });
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
      toast.success(t('skills.resourceDeleted', 'Skill deleted'));
      setConfirmDelete(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };
  const openCustomDialog = () => {
    setCustomBundle(null);
    setCustomDialogOpen(true);
  };
  const saveCustom = async () => {
    try {
      await saveCustomMutation.mutateAsync({
        bundle: customBundle as File,
      });
      toast.success(t('skills.custom.created', 'Custom Skill created'));
      setCustomDialogOpen(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };

  const sourceDescription = source === 'openai'
    ? t('skills.source.openai_desc', 'Curated skills maintained in the public OpenAI skills repository.')
    : t('skills.source.anthropic_desc', 'Public Agent Skill examples maintained by Anthropic.');

  return (
    <>
      <ManagementPageShell
        resourceKind="skill"
        title={t('skills.title', 'Skills')}
        description={t('skills.subtitle', 'Discover and manage reusable instruction packages agents can load on demand.')}
      >

        <Tabs value={tab} onValueChange={changeTab} className="flex min-h-0 flex-1 flex-col gap-4">
          <TabsList variant="underline" className="w-fit shrink-0">
            <TabsTrigger value="all">
              {t('skills.tab.all', 'Skills')}
              <span className="ml-1.5 rounded bg-background/80 px-1.5 py-0.5 text-xs tabular-nums">{items.length}</span>
            </TabsTrigger>
            <TabsTrigger value="discover">{t('skills.tab.discover', 'Discover')}</TabsTrigger>
          </TabsList>

          <TabsContent value="all" className="mt-0 flex min-h-0 flex-1 flex-col gap-4 overflow-hidden data-[state=inactive]:hidden">
            <ManagementToolbar className="flex-col items-stretch">
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative min-w-0 w-full sm:max-w-md sm:flex-1">
                  <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                  <Input data-testid="skill-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('skills.searchAll', 'Search Skills')} className="pl-9" />
                </div>
                <Button onClick={openCustomDialog}><Plus />{t('skills.custom.new', 'Upload Skill Package')}</Button>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Select value={relationship} onValueChange={setRelationship}>
                  <SelectTrigger className="w-44" aria-label={t('skills.relationship.label', 'Resource relationship')}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">{t('skills.relationship.all', 'All relationships')}</SelectItem>
                    <SelectItem value="created">{t('skills.relationship.created', 'Created by me')}</SelectItem>
                    <SelectItem value="shared">{t('skills.relationship.shared', 'Shared with me')}</SelectItem>
                  </SelectContent>
                </Select>
                <Select value={installationState} onValueChange={setInstallationState}>
                  <SelectTrigger className="w-44" aria-label={t('skills.installState.label', 'Installation status')}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">{t('skills.installState.all', 'All installation states')}</SelectItem>
                    <SelectItem value="installed">{t('skills.installState.installed', 'Installed')}</SelectItem>
                    <SelectItem value="uninstalled">{t('skills.installState.uninstalled', 'Not installed')}</SelectItem>
                  </SelectContent>
                </Select>
                <Select value={installedSource} onValueChange={(value) => setInstalledSource(value as InstalledSource)}>
                  <SelectTrigger className="w-48" aria-label={t('skills.filter_source', 'Filter by skill source')}><SelectValue /></SelectTrigger>
                  <SelectContent className="max-h-72">
                    <SelectItem value="all">{t('skills.source.all', 'All Sources')}</SelectItem>
                    <SelectItem value="custom">{t('skills.source.custom', 'Custom')}</SelectItem>
                    <SelectItem value="openai">{t('skills.source.openai', 'OpenAI Curated')}</SelectItem>
                    <SelectItem value="anthropic">{t('skills.source.anthropic', 'Anthropic Public')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </ManagementToolbar>

            <div className="page-scroll-region flex-1 pr-1">
            {skillsQuery.isLoading ? <AsyncState kind="loading" title={t('skills.loading', 'Loading…')} /> : skillsQuery.isError ? (
              <ActionableError
                title={t('skills.load_error', 'Failed To Load Skills.')}
                description={t('skills.loadAllHint', 'Check your connection, then reload Skills.')}
                actionLabel={t('retry', 'Retry')}
                onAction={() => void skillsQuery.refetch()}
                technicalDetails={skillsQuery.error instanceof Error ? skillsQuery.error.message : String(skillsQuery.error)}
                technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
              />
            ) : items.length === 0 ? (
              <CompactEmptyState
                data-testid="skill-empty-state"
                title={t('skills.noAccessible', 'No accessible Skills yet.')}
                description={t('skills.emptyHint', 'Open Discover to choose a verified instruction package.')}
                actionLabel={t('skills.open_discover', 'Open Discover')}
                onAction={() => changeTab('discover')}
              />
            ) : filtered.length === 0 ? (
              <div className="empty-state" data-testid="skill-no-match">
                <div className="empty-state-title">{t('skills.no_match', 'No Skills Match Your Search.')}</div>
                <div className="empty-state-copy">{t('skills.noMatchHint', 'Adjust the search text or source filter.')}</div>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {filtered.map((skill) => <SkillCard key={skill.id} skill={skill} onDelete={setConfirmDelete} />)}
              </div>
            )}
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
                    onChange={(event) => setDiscoverSearch(event.target.value)}
                    placeholder={t('skills.discover.search_ph', 'Search Skills By Task Or Capability')}
                    className="pl-9"
                    data-testid="skill-catalog-search"
                  />
                </div>
                <Button
                  type="submit"
                  className="min-w-28"
                  disabled={isSearchPending}
                  data-testid="skill-catalog-search-button"
                >
                  {isSearchPending ? (
                    <>
                      <Loader2 className="animate-spin" />
                      {t('skills.catalog.searching', 'Searching…')}
                    </>
                  ) : (
                    <>
                      <Search />
                      {t('skills.catalog.search', 'Search')}
                    </>
                  )}
                </Button>
              </form>
              <div className="flex flex-wrap items-center gap-2">
                <Select value={source} onValueChange={changeSource}>
                  <SelectTrigger className="w-56" aria-label={t('skills.select_catalog', 'Select skill catalog')}><SelectValue /></SelectTrigger>
                  <SelectContent className="max-h-72">
                    <SelectItem value="openai">{t('skills.source.openai_repo', 'OpenAI Skills')}</SelectItem>
                    <SelectItem value="anthropic">{t('skills.source.anthropic_repo', 'Anthropic Skills')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">{sourceDescription}</p>
              <div className="mt-3 rounded-lg border border-edge-subtle bg-surface-work p-4">
                <div className="flex items-start gap-3">
                  <BookOpenText className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                  <div>
                    <div className="font-medium">
                      {t('skills.install_guideline.title', 'Install Guideline')}
                    </div>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {t('skills.install_guideline.body', 'Open a Skill to review its instructions, files, tool requirements, and source. Installation validates SKILL.md before making the package available to agents.')}
                    </p>
                  </div>
                </div>
              </div>
            </ManagementToolbar>

            <div className="page-scroll-region flex-1 pr-1">
            {catalogQuery.isLoading ? <AsyncState kind="loading" title={t('skills.catalog.loading', 'Loading Catalog…')} /> : catalogQuery.isError ? (
              <ActionableError
                title={t('skills.catalog.failed', 'Failed To Load The Skill Catalog.')}
                description={t('skills.catalog.failed_hint', 'Check the catalog connection, then try loading it again.')}
                actionLabel={t('retry', 'Retry')}
                onAction={() => void catalogQuery.refetch()}
                technicalDetails={catalogQuery.error instanceof Error ? catalogQuery.error.message : String(catalogQuery.error)}
                technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
              />
            ) : (catalogQuery.data?.items.length ?? 0) === 0 ? (
              <div className="empty-state">
                <div className="empty-state-title">{t('skills.catalog.no_results', 'No Skills Found.')}</div>
                <div className="empty-state-copy">{t('skills.catalog.no_results_hint', 'Try a different task or capability.')}</div>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                {catalogQuery.data?.items.map((item) => <CatalogCard key={`${item.source}:${item.source_id}`} item={item} installed={installedFor(item)} />)}
              </div>
            )}
            {catalogQuery.data?.has_more ? (
              <div className="flex justify-center py-5">
                <Button
                  type="button"
                  variant="outline"
                  data-testid="skill-catalog-more"
                  disabled={catalogQuery.isPlaceholderData || catalogQuery.isFetching}
                  onClick={() => setDiscoverLimit((current) => current + 10)}
                >
                  {catalogQuery.isPlaceholderData || catalogQuery.isFetching
                    ? <Loader2 className="animate-spin" />
                    : <ChevronDown />}
                  {t('skills.catalog.more', 'More')}
                </Button>
              </div>
            ) : null}
            </div>
          </TabsContent>

        </Tabs>
      </ManagementPageShell>

      <Dialog open={!!confirmDelete} onOpenChange={(open) => !open && setConfirmDelete(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('skills.deleteResourceTitle', 'Delete this Skill?')}</DialogTitle>
            <DialogDescription>{t('skills.deleteResourceHint', 'This deletes the resource for all recipients. To remove only your installation, use Uninstall.')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(null)} disabled={deleteMutation.isPending}>{t('skills.cancel', 'Cancel')}</Button>
            <Button variant="destructive" data-testid="skill-confirm-delete" onClick={() => void handleDelete()} disabled={deleteMutation.isPending}>{t('skills.deleteResource', 'Delete Skill')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={customDialogOpen} onOpenChange={setCustomDialogOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {t('skills.custom.create_title', 'Upload Skill Package')}
            </DialogTitle>
            <DialogDescription>
              {t('skills.custom.dialog_help', 'Choose a ZIP package containing SKILL.md at its root. Paths, file types, and size limits are validated before import.')}
            </DialogDescription>
          </DialogHeader>
          <div>
            <div className="rounded-xl border border-dashed border-edge-structural bg-surface-work p-5">
              <label className="flex cursor-pointer items-center gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <FileArchive className="h-5 w-5" />
                </div>
                <span className="min-w-0 flex-1 text-sm">
                  <span className="block font-medium">{customBundle?.name ?? t('skills.custom.choose_zip', 'Choose a Skill ZIP package')}</span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {customBundle
                      ? t('skills.custom.ready', 'Ready to import')
                      : t('skills.custom.zip_hint', 'The archive must contain SKILL.md at its root.')}
                  </span>
                </span>
                <Input
                  type="file"
                  accept=".zip,application/zip"
                  className="hidden"
                  onChange={(event) => setCustomBundle(event.target.files?.[0] ?? null)}
                />
                <span className="rounded-md border px-3 py-1.5 text-xs">{t('skills.custom.browse', 'Browse')}</span>
              </label>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCustomDialogOpen(false)} disabled={saveCustomMutation.isPending}>
              {t('skills.cancel', 'Cancel')}
            </Button>
            <Button onClick={() => void saveCustom()} disabled={saveCustomMutation.isPending || !customBundle}>
              {saveCustomMutation.isPending ? <Loader2 className="animate-spin" /> : null}
              {t('skills.custom.create', 'Create Skill')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
