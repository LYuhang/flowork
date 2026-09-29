import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import { useSearchParams } from 'react-router';
import {
  ChevronRight,
  Download,
  FileText,
  FolderPlus,
  FolderOpen,
  Eye,
  Loader2,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { FileTypeIcon } from '@/components/presentation/FileTypeIcon';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import { Button } from '@/components/ui/button';
import { AsyncState } from '@/components/ui/async-state';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';
import { cn } from '@/lib/utils';
import { resolveFileCapability } from '@/lib/files/capabilities';
import { PaneResizeHandle } from '@/components/ui/pane-resize-handle';
import { usePersistedPaneWidth } from '@/components/ui/use-persisted-pane-width';
import { useFormatDateTime } from '@/lib/timezone';
import { formatBytes } from '@/lib/format/bytes';
import { downloadStorageBlob, type StorageItem } from '@/lib/api/storage';
import {
  useDeleteStorage,
  useMkdirStorage,
  useRenameStorage,
  useStorageContent,
  useStorageList,
  useUploadStorageFile,
  useWriteStorageContent,
} from '@/lib/api/queries/storage';

const ROOTS = [
  { path: '/', label: 'storage', labelKey: 'storage.title' },
  { path: '/mount', label: 'mount', labelKey: 'storage.root.mount' },
  { path: '/workflow', label: 'workflow', labelKey: 'storage.root.workflow' },
  { path: '/project', label: 'project', labelKey: 'storage.root.project' },
  { path: '/task', label: 'task', labelKey: 'storage.root.task' },
];

function parentPath(path: string): string {
  if (path === '/') return '/';
  const parts = path.split('/').filter(Boolean);
  parts.pop();
  return parts.length ? `/${parts.join('/')}` : '/';
}

function joinPath(path: string, name: string): string {
  return `${path === '/' ? '' : path}/${name}`.replace(/\/+/g, '/');
}

function systemDirectoryLabel(
  parentPath: string,
  item: StorageItem,
  t: (key: string, fallback: string) => string,
): string | null {
  if (item.kind !== 'folder' || !/^[0-9a-f-]{24,}$/i.test(item.name)) return null;
  const shortId = item.name.slice(0, 8);
  if (parentPath === '/workflow') {
    return `${t('storage.system.workflowWorkspace', 'Workflow workspace')} · ${shortId}`;
  }
  if (parentPath === '/project') {
    return `${t('storage.system.projectWorkspace', 'Project workspace')} · ${shortId}`;
  }
  if (parentPath === '/task') {
    return `${t('storage.system.taskArtifacts', 'Task artifacts')} · ${shortId}`;
  }
  return null;
}

function pathSegments(path: string, rootLabel: string): { label: string; path: string }[] {
  const parts = path.split('/').filter(Boolean);
  const out = [{ label: rootLabel, path: '/' }];
  let acc = '';
  for (const part of parts) {
    acc += `/${part}`;
    out.push({ label: part, path: acc });
  }
  return out;
}

function displayType(item: StorageItem, t: ReturnType<typeof useTranslation>['t']): string {
  if (item.kind === 'folder') return t('storage.type.folder', 'Folder');
  const file = resolveFileCapability(item.path, item.content_type);
  return file.label === 'Text' ? t('storage.type.text', 'Text') : file.label || t('storage.type.file', 'File');
}

function filename(path: string): string {
  return path.split('/').filter(Boolean).at(-1) || 'download';
}

function MediaPreview({
  path,
  contentType,
  loadingLabel,
  errorLabel,
  retryLabel,
}: {
  path: string;
  contentType?: string | null;
  loadingLabel: string;
  errorLabel: string;
  retryLabel: string;
}) {
  const [attempt, setAttempt] = useState(0);
  const loadKey = `${path}:${attempt}`;
  const [load, setLoad] = useState<{
    key: string;
    objectUrl: string | null;
    error: string | null;
  } | null>(null);
  const capability = resolveFileCapability(path, contentType);

  useEffect(() => {
    let active = true;
    let createdUrl: string | null = null;
    void downloadStorageBlob(path)
      .then((blob) => {
        if (!active) return;
        createdUrl = URL.createObjectURL(blob);
        setLoad({ key: loadKey, objectUrl: createdUrl, error: null });
      })
      .catch((reason) => {
        if (active) {
          setLoad({
            key: loadKey,
            objectUrl: null,
            error: reason instanceof Error ? reason.message : String(reason),
          });
        }
      });
    return () => {
      active = false;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [loadKey, path]);

  const objectUrl = load?.key === loadKey ? load.objectUrl : null;
  const error = load?.key === loadKey ? load.error : null;

  if (error) {
    return (
      <AsyncState
        kind="error"
        className="m-4 flex-1 border-0"
        title={errorLabel}
        description={error}
        actionLabel={retryLabel}
        onAction={() => setAttempt((value) => value + 1)}
      />
    );
  }
  if (!objectUrl) {
    return <AsyncState kind="loading" className="m-4 flex-1 border-0" title={loadingLabel} />;
  }
  if (capability.kind === 'image') {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-surface-sunken p-4">
        <img src={objectUrl} alt={filename(path)} className="max-h-full max-w-full object-contain" />
      </div>
    );
  }
  if (capability.kind === 'audio') {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center bg-surface-sunken p-6">
        <audio
          src={objectUrl}
          controls
          preload="metadata"
          className="w-full max-w-2xl"
          aria-label={filename(path)}
        />
      </div>
    );
  }
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center bg-surface-sunken p-4">
      <video src={objectUrl} controls playsInline className="max-h-full max-w-full" aria-label={filename(path)} />
    </div>
  );
}

export function StoragePage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const formatTime = useFormatDateTime();
  const previewPane = usePersistedPaneWidth({
    storageKey: 'vibecanvas:storage-preview-width:v1',
    defaultWidth: 640,
    minWidth: 480,
    maxWidth: 960,
  });
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const pathParam = searchParams.get('path');
  const path = pathParam?.startsWith('/') ? pathParam : '/';
  const search = searchParams.get('q') ?? '';
  const sortParam = searchParams.get('sort');
  const sort = ['name', 'modified', 'size', 'type'].includes(sortParam ?? '') ? sortParam! : 'name';
  const selectedPathParam = searchParams.get('file');
  const [searchDraftState, setSearchDraftState] = useState({ source: search, value: search });
  const searchDraft = searchDraftState.source === search ? searchDraftState.value : search;
  const setSearchDraft = useCallback((value: string) => {
    setSearchDraftState({ source: search, value });
  }, [search]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<string[]>([]);
  const [previewItem, setPreviewItem] = useState<StorageItem | null>(null);
  const [selectedRowPath, setSelectedRowPath] = useState<string | null>(null);
  const [draftText, setDraftText] = useState('');
  const [editDirty, setEditDirty] = useState(false);
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<StorageItem | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<StorageItem | null>(null);
  const [nameDraft, setNameDraft] = useState('');
  const [pendingTransition, setPendingTransition] = useState<
    | { kind: 'path'; path: string }
    | { kind: 'preview'; item: StorageItem }
    | { kind: 'close-preview' }
    | null
  >(null);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());
  const rowTypeaheadRef = useRef({ value: '', timer: 0 });

  const updateLocation = useCallback((updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(updates)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const list = useStorageList({ path, search, sort, cursor });
  const previewPath = previewItem?.path ?? null;
  const preview = useStorageContent(previewPath);
  const upload = useUploadStorageFile(path);
  const mkdir = useMkdirStorage();
  const rename = useRenameStorage();
  const deleteMutation = useDeleteStorage();
  const write = useWriteStorageContent();

  const canCreate = list.data ? !list.data.readonly : false;
  const segments = useMemo(() => pathSegments(path, t('storage.title', 'Storage')), [path, t]);
  const activeRoot = ROOTS.find((root) => path === root.path || (root.path !== '/' && path.startsWith(`${root.path}/`)))?.path ?? '/';
  const breadcrumbSegments = segments.filter((segment) => segment.path !== '/' && segment.path !== activeRoot);

  useEffect(() => {
    if (searchDraft === search) return;
    const timer = window.setTimeout(() => {
      updateLocation({ q: searchDraft.trim() || null });
      setCursor(null);
      setCursorStack([]);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search, searchDraft, updateLocation]);

  useEffect(() => {
    if (editDirty || selectedPathParam === previewItem?.path) return;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      if (!selectedPathParam) {
        setPreviewItem(null);
        return;
      }
      const listed = list.data?.items.find((item) => item.path === selectedPathParam);
      setSelectedRowPath(selectedPathParam);
      setPreviewItem(listed ?? {
        name: filename(selectedPathParam),
        path: selectedPathParam,
        kind: 'file',
        size_bytes: null,
        modified_at: null,
        content_type: null,
        source: null,
        can_create_child: false,
        can_rename: false,
        can_delete: false,
        can_write: false,
      });
    });
    return () => {
      active = false;
    };
  }, [editDirty, list.data?.items, previewItem?.path, selectedPathParam]);

  useEffect(() => {
    if (!editDirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [editDirty]);

  const commitOpenPath = (next: string) => {
    updateLocation({ path: next === '/' ? null : next, file: null, q: null });
    setSearchDraft('');
    setCursor(null);
    setCursorStack([]);
    setPreviewItem(null);
    setSelectedRowPath(null);
  };

  const openPath = (next: string) => {
    if (editDirty && previewItem) {
      setPendingTransition({ kind: 'path', path: next });
      return;
    }
    commitOpenPath(next);
  };

  const openItem = (item: StorageItem) => {
    if (item.kind === 'folder') {
      openPath(item.path);
      return;
    }
    if (editDirty && previewItem?.path !== item.path) {
      setPendingTransition({ kind: 'preview', item });
      return;
    }
    setPreviewItem(item);
    setSelectedRowPath(item.path);
    updateLocation({ file: item.path });
    setDraftText('');
    setEditDirty(false);
  };

  const requestClosePreview = () => {
    if (editDirty) {
      setPendingTransition({ kind: 'close-preview' });
      return;
    }
    setPreviewItem(null);
    updateLocation({ file: null });
  };

  const discardAndContinue = () => {
    const transition = pendingTransition;
    setPendingTransition(null);
    setEditDirty(false);
    setDraftText('');
    if (!transition || transition.kind === 'close-preview') {
      setPreviewItem(null);
      updateLocation({ file: null });
    } else if (transition.kind === 'path') {
      commitOpenPath(transition.path);
    } else {
      setPreviewItem(transition.item);
      setSelectedRowPath(transition.item.path);
      updateLocation({ file: transition.item.path });
    }
  };

  const openMkdirDialog = () => {
    setNameDraft('');
    setMkdirOpen(true);
  };

  const openRenameDialog = (item: StorageItem) => {
    setRenameTarget(item);
    setNameDraft(item.name);
  };

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const editing = target?.matches('input, textarea, select, [contenteditable="true"]');
      if (editing) return;
      const command = event.metaKey || event.ctrlKey;
      if (command && event.shiftKey && event.key.toLocaleLowerCase() === 'n' && canCreate) {
        event.preventDefault();
        openMkdirDialog();
      } else if (command && event.key.toLocaleLowerCase() === 'u' && canCreate) {
        event.preventDefault();
        fileInputRef.current?.click();
      } else if (event.key === '/') {
        event.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [canCreate]);

  const submitUpload = async (file: File | undefined) => {
    if (!file) return;
    try {
      await upload.mutateAsync(file);
      toast.success(t('storage.toast.uploaded', 'Uploaded'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('storage.toast.uploadFailed', 'Upload failed'));
    }
  };

  const submitMkdir = async () => {
    const name = nameDraft.trim();
    if (!name) return;
    try {
      await mkdir.mutateAsync(joinPath(path, name));
      setMkdirOpen(false);
      setNameDraft('');
      toast.success(t('storage.toast.folderCreated', 'Folder created'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('storage.toast.folderCreateFailed', 'Create failed'));
    }
  };

  const submitRename = async () => {
    if (!renameTarget) return;
    const name = nameDraft.trim();
    if (!name) return;
    if (name === renameTarget.name) {
      setRenameTarget(null);
      setNameDraft('');
      return;
    }
    try {
      const renamed = await rename.mutateAsync({
        old_path: renameTarget.path,
        new_path: joinPath(parentPath(renameTarget.path), name),
      });
      if (previewPath === renameTarget.path) {
        setPreviewItem({ ...renameTarget, name, path: renamed.path });
        setSelectedRowPath(renamed.path);
        updateLocation({ file: renamed.path });
      }
      setRenameTarget(null);
      setNameDraft('');
      toast.success(t('storage.toast.renamed', 'Renamed'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('storage.toast.renameFailed', 'Rename failed'));
    }
  };

  const submitDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteMutation.mutateAsync(deleteTarget.path);
      if (previewPath === deleteTarget.path) {
        setPreviewItem(null);
        updateLocation({ file: null });
      }
      setDeleteTarget(null);
      toast.success(t('storage.toast.deleted', 'Deleted'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('storage.toast.deleteFailed', 'Delete failed'));
    }
  };

  const submitDownload = async (item: StorageItem) => {
    try {
      const blob = await downloadStorageBlob(item.path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = item.name || filename(item.path);
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('storage.toast.downloadFailed', 'Download failed'));
    }
  };

  const submitSavePreview = async () => {
    if (!previewPath || !preview.data) return false;
    try {
      await write.mutateAsync({
        path: previewPath,
        content: draftText,
        content_type: preview.data.content_type,
      });
      setEditDirty(false);
      toast.success(t('storage.toast.saved', 'Saved'));
      return true;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('storage.toast.saveFailed', 'Save failed'));
      return false;
    }
  };

  const previewText = editDirty ? draftText : preview.data?.content ?? '';
  const previewCapability = resolveFileCapability(
    previewPath ?? '',
    preview.data?.content_type ?? previewItem?.content_type,
  );
  const previewCanEdit = !!previewPath
    && !!previewItem?.can_write
    && !!preview.data
    && previewCapability.editable
    && !preview.data.truncated;
  const visibleItems = list.data?.items ?? [];

  const focusRow = (item: StorageItem) => {
    setSelectedRowPath(item.path);
    rowRefs.current.get(item.path)?.focus();
  };

  const handleRowKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    item: StorageItem,
  ) => {
    const index = visibleItems.findIndex((candidate) => candidate.path === item.path);
    const focusAt = (nextIndex: number) => {
      const next = visibleItems[nextIndex];
      if (next) focusRow(next);
    };
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      focusAt(Math.min(visibleItems.length - 1, index + 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      focusAt(Math.max(0, index - 1));
    } else if (event.key === 'Home') {
      event.preventDefault();
      focusAt(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      focusAt(visibleItems.length - 1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      openItem(item);
    } else if (event.key === 'Backspace') {
      event.preventDefault();
      openPath(parentPath(path));
    } else if (event.key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey) {
      window.clearTimeout(rowTypeaheadRef.current.timer);
      const query = `${rowTypeaheadRef.current.value}${event.key}`.toLocaleLowerCase();
      rowTypeaheadRef.current.value = query;
      rowTypeaheadRef.current.timer = window.setTimeout(() => {
        rowTypeaheadRef.current.value = '';
      }, 600);
      const ordered = [...visibleItems.slice(index + 1), ...visibleItems.slice(0, index + 1)];
      const match = ordered.find((candidate) => candidate.name.toLocaleLowerCase().startsWith(query));
      if (match) focusRow(match);
    }
  };

  return (
    <div
      className="flex h-full min-h-0 bg-background text-foreground"
      data-page-archetype="editor-workspace"
      data-edit-state={previewCanEdit ? (editDirty ? 'dirty' : 'saved') : 'view'}
    >
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="surface-topbar flex min-h-12 shrink-0 flex-col gap-2 px-3 py-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between sm:px-4">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <ResourceIcon kind="storage" size="sm" />
            <Select value={activeRoot} onValueChange={openPath}>
              <SelectTrigger
                className="h-8 w-[132px] shrink-0 border-edge-subtle bg-surface-raised text-ui shadow-none"
                aria-label={t('storage.roots', 'Storage root')}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start">
                {ROOTS.map((root) => (
                  <SelectItem key={root.path} value={root.path}>
                    {t(root.labelKey, root.label)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="flex min-w-0 items-center gap-1 text-ui">
            {breadcrumbSegments.map((seg) => (
              <span key={seg.path} className="flex min-w-0 items-center gap-1">
                <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <button
                  type="button"
                  className={cn(
                    'min-w-6 max-w-40 truncate rounded px-1.5 py-1 transition-colors hover:bg-muted',
                    seg.path === breadcrumbSegments.at(-1)?.path && 'font-medium',
                  )}
                  onClick={() => openPath(seg.path)}
                >
                  {seg.label}
                </button>
              </span>
            ))}
            </div>
          </div>
          <div className="flex shrink-0 items-center justify-end gap-2">
            <Button variant="outline" size="sm" disabled={!canCreate} onClick={openMkdirDialog}>
              <FolderPlus className="size-4" />
              {t('storage.newFolder', 'New folder')}
            </Button>
            <Button size="sm" disabled={!canCreate || upload.isPending} onClick={() => fileInputRef.current?.click()}>
              {upload.isPending ? <Loader2 className="size-4 animate-spin" /> : <Upload className="size-4" />}
              {t('upload', 'Upload')}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="toolbar-icon-button"
              onClick={() => void list.refetch()}
              title={t('refresh', 'Refresh')}
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              onChange={(e) => {
                void submitUpload(e.target.files?.[0]);
                e.currentTarget.value = '';
              }}
            />
          </div>
        </header>

        <div className="flex shrink-0 items-center gap-2 border-b bg-surface-sunken/35 px-3 py-2.5 sm:px-4">
          <div className="relative min-w-0 flex-1 sm:max-w-72">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              ref={searchInputRef}
              value={searchDraft}
              onChange={(e) => {
                setSearchDraft(e.target.value);
                setCursor(null);
                setCursorStack([]);
              }}
              placeholder={t('storage.search', 'Search current folder')}
              className="h-9 pl-8 text-ui"
            />
          </div>
          <Select
            value={sort}
            onValueChange={(value) => {
              updateLocation({ sort: value === 'name' ? null : value });
              setCursor(null);
              setCursorStack([]);
            }}
          >
            <SelectTrigger className="h-9 w-36 text-ui" aria-label={t('storage.sort.label', 'Sort files')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="name">{t('storage.sort.name', 'Name')}</SelectItem>
              <SelectItem value="modified">{t('storage.sort.modified', 'Modified')}</SelectItem>
              <SelectItem value="size">{t('storage.sort.size', 'Size')}</SelectItem>
              <SelectItem value="type">{t('storage.sort.type', 'Type')}</SelectItem>
            </SelectContent>
          </Select>
          <div className="ml-auto text-meta">
            {list.data?.total_estimate ?? 0} {t('storage.items', 'items')}
          </div>
        </div>

        <ContextMenu>
          <ContextMenuTrigger asChild>
            <div className="app-scrollbar min-h-0 flex-1 overflow-auto bg-background">
              <table className="w-full table-fixed text-ui">
                <thead className="sticky top-0 z-sticky border-b border-edge-subtle bg-surface-sunken">
                  <tr className="text-left text-xs font-medium text-muted-foreground">
                    <th className="w-full px-3 py-2 font-medium sm:px-4 md:w-[48%]">{t('storage.name', 'Name')}</th>
                    <th className="hidden w-[18%] px-3 py-2 font-medium md:table-cell">{t('storage.type', 'Type')}</th>
                    <th className="hidden w-[14%] px-3 py-2 font-medium lg:table-cell">{t('storage.size', 'Size')}</th>
                    <th className="hidden w-[20%] px-3 py-2 font-medium md:table-cell">{t('storage.modified', 'Modified')}</th>
                  </tr>
                </thead>
                <tbody>
                  {list.isLoading && Array.from({ length: 8 }).map((_v, i) => (
                    <tr key={i} className="border-b">
                      <td className="px-3 py-3 sm:px-4"><Skeleton className="h-4 w-48 max-w-full sm:w-64" /></td>
                      <td className="hidden px-3 py-3 md:table-cell"><Skeleton className="h-4 w-20" /></td>
                      <td className="hidden px-3 py-3 lg:table-cell"><Skeleton className="h-4 w-16" /></td>
                      <td className="hidden px-3 py-3 md:table-cell"><Skeleton className="h-4 w-28" /></td>
                    </tr>
                  ))}
                  {!list.isLoading && visibleItems.map((item) => {
                    const friendlyName = systemDirectoryLabel(path, item, t);
                    return (
                      <ContextMenu key={item.path}>
                        <ContextMenuTrigger asChild>
                          <tr
                            aria-selected={selectedRowPath === item.path}
                            className={cn(
                              'interactive-row group border-b data-[state=open]:bg-surface-hover',
                              selectedRowPath === item.path && 'bg-surface-hover',
                            )}
                            onClick={() => openItem(item)}
                            onDoubleClick={() => openItem(item)}
                          >
                            <td className="min-w-0 px-3 py-2 sm:px-4">
                              <div className="flex min-w-0 items-center gap-2">
                              <button
                                type="button"
                                ref={(element) => {
                                  if (element) rowRefs.current.set(item.path, element);
                                  else rowRefs.current.delete(item.path);
                                }}
                                tabIndex={selectedRowPath === item.path || (!selectedRowPath && item === visibleItems[0]) ? 0 : -1}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  openItem(item);
                                }}
                                onKeyDown={(event) => handleRowKeyDown(event, item)}
                                title={friendlyName ? `${friendlyName}\n${item.name}` : item.name}
                                className="flex min-h-8 min-w-0 w-full items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
                              >
                                <FileTypeIcon
                                  fileName={item.name}
                                  mimeType={item.content_type}
                                  directory={item.kind === 'folder'}
                                  className="size-7"
                                />
                                <span className="min-w-0">
                                  <span className="block truncate font-medium">{friendlyName ?? item.name}</span>
                                  {friendlyName ? (
                                    <span className="block truncate font-mono text-xs text-content-tertiary">{item.name}</span>
                                  ) : null}
                                </span>
                              </button>
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <Button variant="ghost" size="icon" className="shrink-0" aria-label={`${t('actions', 'Actions')}: ${item.name}`} onClick={(event) => event.stopPropagation()}>
                                    <MoreHorizontal className="size-4" />
                                  </Button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent align="end" onClick={(event) => event.stopPropagation()}>
                                  <DropdownMenuItem onSelect={() => openItem(item)}>
                                    {item.kind === 'folder' ? <FolderOpen /> : <Eye />}
                                    {item.kind === 'folder' ? t('open', 'Open') : t('view', 'View')}
                                  </DropdownMenuItem>
                                  {item.kind === 'file' && <DropdownMenuItem onSelect={() => void submitDownload(item)}><Download />{t('download', 'Download')}</DropdownMenuItem>}
                                  {(item.can_rename || item.can_delete) && <DropdownMenuSeparator />}
                                  {item.can_rename && <DropdownMenuItem onSelect={() => openRenameDialog(item)}><Pencil />{t('rename', 'Rename')}</DropdownMenuItem>}
                                  {item.can_delete && <DropdownMenuItem className="text-destructive focus:text-destructive" onSelect={() => setDeleteTarget(item)}><Trash2 />{t('delete', 'Delete')}</DropdownMenuItem>}
                                </DropdownMenuContent>
                              </DropdownMenu>
                              </div>
                            </td>
                            <td className="hidden px-3 py-2 text-meta md:table-cell">{displayType(item, t)}</td>
                            <td className="hidden px-3 py-2 text-meta lg:table-cell">
                              {item.kind === 'folder' ? '—' : formatBytes(item.size_bytes ?? 0)}
                            </td>
                            <td className="hidden px-3 py-2 text-meta md:table-cell">
                              {formatTime(item.modified_at)}
                            </td>
                          </tr>
                        </ContextMenuTrigger>
                        <ContextMenuContent className="w-44">
                          <ContextMenuItem onSelect={() => openItem(item)}>
                            {item.kind === 'folder' ? (
                              <FolderOpen className="mr-2 h-4 w-4" />
                            ) : (
                              <Eye className="mr-2 h-4 w-4" />
                            )}
                            {item.kind === 'folder' ? t('open', 'Open') : t('view', 'View')}
                          </ContextMenuItem>
                          {item.kind === 'file' && (
                            <ContextMenuItem onSelect={() => void submitDownload(item)}>
                              <Download className="mr-2 h-4 w-4" />
                              {t('download', 'Download')}
                            </ContextMenuItem>
                          )}
                          {(item.can_rename || item.can_delete) && <ContextMenuSeparator />}
                          {item.can_rename && (
                            <ContextMenuItem onSelect={() => openRenameDialog(item)}>
                              <Pencil className="mr-2 h-4 w-4" />
                              {t('rename', 'Rename')}
                            </ContextMenuItem>
                          )}
                          {item.can_delete && (
                            <ContextMenuItem
                              className="text-destructive focus:text-destructive"
                              onSelect={() => setDeleteTarget(item)}
                            >
                              <Trash2 className="mr-2 h-4 w-4" />
                              {t('delete', 'Delete')}
                            </ContextMenuItem>
                          )}
                        </ContextMenuContent>
                      </ContextMenu>
                    );
                  })}
                </tbody>
              </table>
              {list.isError && (
                <AsyncState
                  kind="error"
                  className="m-4"
                  title={t('storage.loadError', 'Could not load this folder')}
                  description={t('storage.loadErrorHint', 'Check the connection and try loading this folder again.')}
                  technicalDetails={list.error instanceof Error ? list.error.message : undefined}
                  technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
                  actionLabel={t('retry', 'Retry')}
                  onAction={() => void list.refetch()}
                />
              )}
              {!list.isLoading && !list.isError && (list.data?.items.length ?? 0) === 0 && (
                <div className="empty-state m-4">
                  <FileText className="h-7 w-7" />
                  <div className="empty-state-title">{t('storage.emptyFolder', 'No files in this folder')}</div>
                  <div className="empty-state-copy">
                    {canCreate
                      ? t('storage.emptyFolderHint', 'Right-click the empty area to create a folder or upload a file.')
                      : t('storage.emptyReadonlyHint', 'This location is managed by the workspace.')}
                  </div>
                </div>
              )}
            </div>
          </ContextMenuTrigger>
          <ContextMenuContent className="w-48">
            <ContextMenuItem disabled={!canCreate} onSelect={openMkdirDialog}>
              <FolderPlus className="mr-2 h-4 w-4" />
              {t('storage.newFolder', 'New folder')}
            </ContextMenuItem>
            <ContextMenuItem disabled={!canCreate} onSelect={() => fileInputRef.current?.click()}>
              <Upload className="mr-2 h-4 w-4" />
              {t('upload', 'Upload')}
            </ContextMenuItem>
            <ContextMenuSeparator />
            <ContextMenuItem onSelect={() => void list.refetch()}>
              <RefreshCw className="mr-2 h-4 w-4" />
              {t('refresh', 'Refresh')}
            </ContextMenuItem>
          </ContextMenuContent>
        </ContextMenu>

        <footer className="flex h-11 shrink-0 items-center justify-between border-t bg-muted/20 px-4">
          <Button
            variant="ghost"
            size="sm"
            disabled={cursorStack.length === 0}
            onClick={() => {
              const next = [...cursorStack];
              const prev = next.pop() ?? null;
              setCursor(prev);
              setCursorStack(next);
            }}
          >
            {t('previous', 'Previous')}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={!list.data?.next_cursor}
            onClick={() => {
              setCursorStack((s) => [...s, cursor ?? '0']);
              setCursor(list.data?.next_cursor ?? null);
            }}
          >
            {t('next', 'Next')}
          </Button>
        </footer>
      </section>

      {previewPath ? (
        <aside
          className="pane-enter-from-right surface-sidepanel fixed inset-0 z-auxiliary flex w-full shrink-0 flex-col rounded-none border-y-0 border-r-0 md:relative md:inset-auto md:z-auto md:w-[var(--storage-preview-width)]"
          style={{ '--storage-preview-width': `${previewPane.width}px` } as CSSProperties}
          aria-label={t('storage.preview', 'File preview')}
        >
          <PaneResizeHandle
            side="left"
            width={previewPane.width}
            minWidth={480}
            maxWidth={960}
            onWidthChange={previewPane.setWidth}
            onReset={previewPane.resetWidth}
            label={t('storage.resizePreview', 'Resize file preview')}
            className="hidden md:block"
          />
          <div className="border-b px-4 py-3">
            <div className="flex items-center justify-between gap-2">
              <h2 className="truncate text-section">{previewPath}</h2>
              <Button
                variant="quiet"
                size="icon"
                className="toolbar-icon-button"
                aria-label={t('storage.closePreview', 'Close file preview')}
                onClick={requestClosePreview}
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <div className="flex min-h-0 flex-1 flex-col">
            {preview.isLoading && (
              <div className="p-4">
                <Skeleton className="mb-3 h-4 w-1/2" />
                <Skeleton className="h-80 w-full" />
              </div>
            )}
            {preview.isError && (
              <AsyncState
                kind="error"
                className="m-4 flex-1 border-0"
                title={t('storage.previewLoadError', 'Could not load this preview')}
                description={t('storage.previewLoadErrorHint', 'The file may have changed or the connection was interrupted. Try loading it again.')}
                technicalDetails={preview.error instanceof Error ? preview.error.message : undefined}
                technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
                actionLabel={t('retry', 'Retry')}
                onAction={() => void preview.refetch()}
              />
            )}
            {preview.data && (previewCapability.kind === 'image' || previewCapability.kind === 'audio' || previewCapability.kind === 'video') && (
              <MediaPreview
                path={preview.data.path}
                contentType={preview.data.content_type}
                loadingLabel={t('storage.previewLoading', 'Loading preview…')}
                errorLabel={t('storage.previewLoadError', 'Could not load this preview')}
                retryLabel={t('retry', 'Retry')}
              />
            )}
            {preview.data && !previewCapability.source && previewCapability.kind !== 'image' && previewCapability.kind !== 'audio' && previewCapability.kind !== 'video' && (
              <div className="empty-state flex-1 border-0">
                <FileTypeIcon
                  fileName={previewPath}
                  mimeType={preview.data.content_type}
                  className="size-10"
                />
                <div className="text-ui">{t('storage.binaryPreview', 'This file cannot be previewed as text.')}</div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => previewPath && void submitDownload({
                    name: filename(previewPath),
                    path: previewPath,
                    kind: 'file',
                    size_bytes: preview.data?.size_bytes ?? 0,
                    modified_at: null,
                    content_type: preview.data?.content_type ?? null,
                    source: null,
                    can_create_child: false,
                    can_rename: false,
                    can_delete: false,
                    can_write: false,
                  })}
                >
                  <Download className="mr-2 h-4 w-4" />
                  {t('download', 'Download')}
                </Button>
              </div>
            )}
            {preview.data && previewCapability.source && (
              <>
                <div className="flex items-center justify-between border-b px-4 py-2 text-meta">
                  <span>
                    {displayType({
                      name: filename(preview.data.path),
                      path: preview.data.path,
                      kind: 'file',
                      size_bytes: preview.data.size_bytes,
                      modified_at: null,
                      content_type: preview.data.content_type,
                      source: null,
                      can_create_child: false,
                      can_rename: false,
                      can_delete: false,
                      can_write: false,
                    }, t)} · {formatBytes(preview.data.size_bytes)}
                    {preview.data.truncated ? ` · ${t('storage.truncated', 'truncated')}` : ''}
                  </span>
                  {previewCanEdit ? (
                    <Button
                      size="sm"
                      disabled={!editDirty || write.isPending}
                      onClick={() => void submitSavePreview()}
                    >
                      {write.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                      {t('save', 'Save')}
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => void submitDownload({
                        name: filename(preview.data.path),
                        path: preview.data.path,
                        kind: 'file',
                        size_bytes: preview.data.size_bytes,
                        modified_at: null,
                        content_type: preview.data.content_type,
                        source: null,
                        can_create_child: false,
                        can_rename: false,
                        can_delete: false,
                        can_write: false,
                      })}
                    >
                      <Download className="mr-2 h-4 w-4" />
                      {t('download', 'Download')}
                    </Button>
                  )}
                </div>
                <Textarea
                  value={previewText}
                  onChange={(e) => {
                    if (!previewCanEdit) return;
                    if (!editDirty) setDraftText(preview.data?.content ?? '');
                    setDraftText(e.target.value);
                    setEditDirty(true);
                  }}
                  readOnly={!previewCanEdit}
                  spellCheck={false}
                  className="app-scrollbar min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-[13px] leading-5 focus-visible:ring-0"
                />
              </>
            )}
          </div>
        </aside>
      ) : null}

      <Dialog open={pendingTransition !== null} onOpenChange={(open) => !open && setPendingTransition(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('storage.unsavedTitle', 'Unsaved file changes')}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('storage.unsavedDescription', 'Save or discard your changes before leaving this file.')}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingTransition(null)}>
              {t('storage.continueEditing', 'Continue editing')}
            </Button>
            <Button variant="danger" onClick={discardAndContinue}>
              {t('storage.discard', 'Discard changes')}
            </Button>
            <Button
              disabled={!previewCanEdit || write.isPending}
              onClick={async () => {
                if (await submitSavePreview()) discardAndContinue();
              }}
            >
              {write.isPending ? t('storage.saving', 'Saving…') : t('save', 'Save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={mkdirOpen} onOpenChange={setMkdirOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('storage.newFolder', 'New folder')}</DialogTitle></DialogHeader>
          <Input value={nameDraft} onChange={(e) => setNameDraft(e.target.value)} placeholder="folder-name" />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setMkdirOpen(false)}>{t('cancel', 'Cancel')}</Button>
            <Button onClick={() => void submitMkdir()} disabled={mkdir.isPending}>{t('create', 'Create')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!renameTarget} onOpenChange={(open) => !open && setRenameTarget(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('storage.rename', 'Rename')}</DialogTitle></DialogHeader>
          <Input value={nameDraft} onChange={(e) => setNameDraft(e.target.value)} />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRenameTarget(null)}>{t('cancel', 'Cancel')}</Button>
            <Button
              onClick={() => void submitRename()}
              disabled={rename.isPending || !nameDraft.trim() || nameDraft.trim() === renameTarget?.name}
            >
              {t('rename', 'Rename')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!deleteTarget} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('storage.deleteTitle', 'Delete item')}</DialogTitle></DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t('storage.deleteConfirm', 'Delete this item and its contents?')} {deleteTarget?.name}
          </p>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)}>{t('cancel', 'Cancel')}</Button>
            <Button variant="destructive" onClick={() => void submitDelete()} disabled={deleteMutation.isPending}>
              {t('delete', 'Delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
