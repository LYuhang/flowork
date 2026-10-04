import { useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FileArchive, FolderUp, Plus, Search } from 'lucide-react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { ManagementPageShell } from '@/components/layout/management-page-shell';
import { ActionableError } from '@/components/presentation/ActionableError';
import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { createKb, importKb, listKbs, type ImportKbSource } from '@/lib/api/kb';
import { useFormatDateTime } from '@/lib/timezone';


const knowledgeKey = ['knowledge-bases'] as const;
type KnowledgeSort = 'updated' | 'created' | 'name';

function errorState(error: unknown): 'permission' | 'error' {
  const message = error instanceof Error ? error.message : String(error ?? '');
  return /(?:\b403\b|forbidden|permission)/i.test(message) ? 'permission' : 'error';
}

export function KnowledgeListPage() {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [importName, setImportName] = useState('');
  const [importDescription, setImportDescription] = useState('');
  const [importSource, setImportSource] = useState<ImportKbSource | null>(null);
  const [importError, setImportError] = useState('');
  const folderInput = useRef<HTMLInputElement>(null);
  const archiveInput = useRef<HTMLInputElement>(null);
  const [sort, setSort] = useState<KnowledgeSort>('updated');
  const scope = searchParams.get('scope') ?? 'all';
  const knowledge = useQuery({
    queryKey: knowledgeKey,
    queryFn: listKbs,
  });
  const create = useMutation({
    mutationFn: () => createKb({ name: name.trim(), description: description.trim() || undefined }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: knowledgeKey });
      setCreateOpen(false);
      setName('');
      setDescription('');
      toast.success(t('knowledge.created', 'Knowledge base created'));
    },
    onError: (reason) => toast.error(reason instanceof Error ? reason.message : String(reason)),
  });
  const importPackage = useMutation({
    mutationFn: () => {
      if (!importSource) throw new Error(t('knowledge.import.sourceRequired', 'Choose a folder or ZIP archive.'));
      return importKb(
        { name: importName.trim(), description: importDescription.trim() || undefined },
        importSource,
      );
    },
    onSuccess: async (created) => {
      await client.invalidateQueries({ queryKey: knowledgeKey });
      setImportOpen(false);
      setImportName('');
      setImportDescription('');
      setImportSource(null);
      setImportError('');
      toast.success(t('knowledge.import.created', 'Knowledge folder imported'));
      navigate(`/knowledge/${created.id}`);
    },
    onError: (reason) => toast.error(reason instanceof Error ? reason.message : String(reason)),
  });

  const selectFolder = (selected: File[]) => {
    const paths = selected.map((file) => file.webkitRelativePath || file.name);
    const roots = new Set(paths.map((path) => path.split('/').filter(Boolean)[0]));
    const wrapper = roots.size === 1 ? [...roots][0] : '';
    const logicalPaths = paths.map((path) => wrapper && path.startsWith(`${wrapper}/`)
      ? path.slice(wrapper.length + 1)
      : path);
    const hasRootReadme = logicalPaths.some((path) => path.toLocaleLowerCase() === 'readme.md');
    setImportSource({ kind: 'folder', files: selected, paths });
    setImportError(hasRootReadme ? '' : t('knowledge.import.readmeRequired', 'The selected folder must contain README.md at its root.'));
    if (!importName.trim() && wrapper) setImportName(wrapper);
  };
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (knowledge.data ?? [])
      .filter((item) => scope === 'created' ? item.created_by_me : scope === 'shared' ? !item.created_by_me : true)
      .filter((item) => (
        !normalized
        || `${item.name} ${item.description ?? ''}`.toLocaleLowerCase().includes(normalized)
      ))
      .sort((left, right) => {
        if (sort === 'name') return left.name.localeCompare(right.name);
        const leftDate = Date.parse(sort === 'created' ? left.created_at : left.latest_updated_at);
        const rightDate = Date.parse(sort === 'created' ? right.created_at : right.latest_updated_at);
        return rightDate - leftDate;
      });
  }, [knowledge.data, query, sort, scope]);
  const hasActiveFilters = Boolean(query.trim());
  const setScope = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value !== 'all') next.set('scope', value);
    else next.delete('scope');
    setSearchParams(next, { replace: true });
  };

  return (
    <ManagementPageShell
      resourceKind="knowledge"
      title={t('knowledge.title', 'Knowledge')}
      description={t('knowledge.description', 'Keep reusable notes, documents, and media in file-based packages the Agent can progressively explore.')}
      actions={<>
        <Button variant="outline" onClick={() => setImportOpen(true)}><FolderUp className="h-4 w-4" />{t('knowledge.import.action', 'Upload folder')}</Button>
        <Button onClick={() => setCreateOpen(true)}><Plus className="h-4 w-4" />{t('knowledge.create', 'New knowledge base')}</Button>
      </>}
    >

      <div className="flex flex-col gap-3">
        <div className="relative min-w-64 flex-1 sm:max-w-md">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-content-tertiary" />
          <Input value={query} onChange={(event) => setQuery(event.target.value)} className="pl-9" placeholder={t('knowledge.searchList', 'Search knowledge bases')} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={scope} onValueChange={setScope}>
            <SelectTrigger className="w-48" aria-label={t('skills.relationship.label', 'Resource relationship')}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('skills.relationship.all', 'All relationships')}</SelectItem>
              <SelectItem value="created">{t('skills.relationship.created', 'Created by me')}</SelectItem>
              <SelectItem value="shared">{t('skills.relationship.shared', 'Shared with me')}</SelectItem>
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={(value) => setSort(value as KnowledgeSort)}>
            <SelectTrigger className="w-40" aria-label={t('knowledge.sort', 'Sort knowledge bases')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="updated">{t('knowledge.sort.updated', 'Recently updated')}</SelectItem>
              <SelectItem value="created">{t('knowledge.sort.created', 'Newest created')}</SelectItem>
              <SelectItem value="name">{t('knowledge.sort.name', 'Name')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      {knowledge.isLoading ? <AsyncState kind="loading" title={t('knowledge.loading', 'Loading knowledge bases…')} /> : null}
      {knowledge.isError && errorState(knowledge.error) === 'permission' ? (
        <AsyncState
          kind="permission"
          title={t('knowledge.forbidden', 'You do not have access to Knowledge')}
        />
      ) : null}
      {knowledge.isError && errorState(knowledge.error) === 'error' ? (
        <ActionableError
          title={t('knowledge.loadFailed', 'Could not load knowledge bases')}
          description={t('knowledge.loadFailedHint', 'Check the connection and try loading the list again.')}
          actionLabel={t('retry', 'Retry')}
          onAction={() => void knowledge.refetch()}
          technicalDetails={knowledge.error instanceof Error ? knowledge.error.message : String(knowledge.error)}
          technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
        />
      ) : null}
      {!knowledge.isLoading && !knowledge.isError && !filtered.length ? (
        <CompactEmptyState
          title={hasActiveFilters ? t('knowledge.noMatch', 'No matching knowledge bases') : t('knowledge.empty', 'No knowledge bases yet')}
          description={hasActiveFilters ? t('knowledge.noMatchHint', 'Try a different name or clear the search.') : t('knowledge.emptyHint', 'Create a note package, describe it in README.md, then add any supporting files.')}
          actionLabel={!hasActiveFilters ? t('knowledge.create', 'New knowledge base') : undefined}
          onAction={!hasActiveFilters ? () => setCreateOpen(true) : undefined}
        />
      ) : null}
      {filtered.length ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {filtered.map((item) => (
            <Link key={item.id} to={`/knowledge/${item.id}`} className="group flex min-w-0 min-h-44 flex-col rounded-xl border border-edge-subtle bg-surface-raised p-4 shadow-sm transition hover:-translate-y-0.5 hover:border-edge-strong hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus">
              <span className="flex items-start justify-between gap-3">
                <ResourceIcon kind="knowledge" size="lg" className="size-10 rounded-lg" />
                <span className="rounded-md bg-surface-sunken px-2 py-1 text-xs font-medium tabular-nums text-content-secondary">
                  v{item.package_version}
                </span>
              </span>
              <span className="mt-4 min-w-0 flex-1">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-base font-semibold text-content-primary">{item.name}</span>
                </span>
                <span className="mt-1 [overflow-wrap:anywhere] line-clamp-2 text-sm leading-5 text-content-secondary">{item.description || t('knowledge.noDescription', 'No description')}</span>
                <span className="mt-3 block text-xs text-content-tertiary">
                  {item.file_count} {t('knowledge.files', 'files')} · {t('knowledge.updatedAt', 'Updated {{time}}', { time: formatTime(item.latest_updated_at) })}
                </span>
                <ResourceProvenanceLine provenance={item.provenance} className="mt-1 flex" />
              </span>
            </Link>
          ))}
        </div>
      ) : null}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('knowledge.create', 'New knowledge base')}</DialogTitle><DialogDescription>{t('knowledge.createHint', 'Name the collection by the questions it should answer.')}</DialogDescription></DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5"><Label htmlFor="kb-name">{t('name', 'Name')}</Label><Input id="kb-name" value={name} onChange={(event) => setName(event.target.value)} autoFocus /></div>
            <div className="space-y-1.5"><Label htmlFor="kb-description">{t('description', 'Description')}</Label><Textarea id="kb-description" value={description} onChange={(event) => setDescription(event.target.value)} /></div>
          </div>
          <DialogFooter><Button variant="outline" onClick={() => setCreateOpen(false)}>{t('cancel', 'Cancel')}</Button><Button disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>{create.isPending ? t('creating', 'Creating…') : t('create', 'Create')}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={importOpen} onOpenChange={(open) => {
        if (!importPackage.isPending) setImportOpen(open);
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('knowledge.import.title', 'Upload a knowledge folder')}</DialogTitle>
            <DialogDescription>{t('knowledge.import.hint', 'Choose a complete folder or ZIP archive. Its root README.md describes the package and its contents.')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid gap-2 sm:grid-cols-2">
              <Button type="button" variant="outline" className="h-auto justify-start p-3" onClick={() => folderInput.current?.click()}>
                <FolderUp className="size-5" />
                <span className="text-left"><span className="block">{t('knowledge.import.folder', 'Choose folder')}</span><span className="block text-xs font-normal text-content-tertiary">{t('knowledge.import.folderHint', 'Preserves nested directories')}</span></span>
              </Button>
              <Button type="button" variant="outline" className="h-auto justify-start p-3" onClick={() => archiveInput.current?.click()}>
                <FileArchive className="size-5" />
                <span className="text-left"><span className="block">{t('knowledge.import.zip', 'Choose ZIP')}</span><span className="block text-xs font-normal text-content-tertiary">{t('knowledge.import.zipHint', 'Imports one packaged folder')}</span></span>
              </Button>
              <input
                ref={(element) => {
                  folderInput.current = element;
                  element?.setAttribute('webkitdirectory', '');
                }}
                type="file"
                multiple
                className="hidden"
                data-testid="knowledge-import-folder-input"
                aria-label={t('knowledge.import.folder', 'Choose folder')}
                onChange={(event) => {
                  const selected = Array.from(event.currentTarget.files ?? []);
                  if (selected.length) selectFolder(selected);
                  event.currentTarget.value = '';
                }}
              />
              <input
                ref={archiveInput}
                type="file"
                accept=".zip,application/zip"
                className="hidden"
                data-testid="knowledge-import-zip-input"
                aria-label={t('knowledge.import.zip', 'Choose ZIP')}
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  if (file) {
                    setImportSource({ kind: 'archive', file });
                    setImportError('');
                    if (!importName.trim()) setImportName(file.name.replace(/\.zip$/i, ''));
                  }
                  event.currentTarget.value = '';
                }}
              />
            </div>
            {importSource ? (
              <div className="rounded-md border border-edge-subtle bg-surface-sunken px-3 py-2 text-sm">
                <span className="font-medium">{importSource.kind === 'archive' ? importSource.file.name : t('knowledge.import.folderSelected', '{{count}} files selected', { count: importSource.files.length })}</span>
                {importError ? <p className="mt-1 text-xs text-destructive">{importError}</p> : null}
              </div>
            ) : null}
            <div className="space-y-1.5"><Label htmlFor="kb-import-name">{t('name', 'Name')}</Label><Input id="kb-import-name" value={importName} onChange={(event) => setImportName(event.target.value)} /></div>
            <div className="space-y-1.5"><Label htmlFor="kb-import-description">{t('description', 'Description')}</Label><Textarea id="kb-import-description" value={importDescription} onChange={(event) => setImportDescription(event.target.value)} /></div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setImportOpen(false)} disabled={importPackage.isPending}>{t('cancel', 'Cancel')}</Button>
            <Button disabled={!importName.trim() || !importSource || Boolean(importError) || importPackage.isPending} onClick={() => importPackage.mutate()}>
              {importPackage.isPending ? t('knowledge.import.importing', 'Importing…') : t('knowledge.import.submit', 'Import knowledge')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ManagementPageShell>
  );
}
