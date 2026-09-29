import { useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import { ChevronRight, FolderUp, MoreHorizontal, Trash2, Upload } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { FileTypeIcon } from '@/components/presentation/FileTypeIcon';
import { Button } from '@/components/ui/button';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { PaneResizeHandle } from '@/components/ui/pane-resize-handle';
import { usePersistedPaneWidth } from '@/components/ui/use-persisted-pane-width';
import { type KbFile } from '@/lib/api/kb';
import { cn } from '@/lib/utils';
import { KnowledgeFilePreview } from '@/pages/knowledge/KnowledgeFilePreview';

type TreeNode = {
  name: string;
  path: string;
  kind: 'folder' | 'file';
  file?: KbFile;
  children: TreeNode[];
};

type VisibleItem = { node: TreeNode; depth: number; parentPath: string | null };

function buildTree(files: KbFile[]): TreeNode[] {
  const roots: TreeNode[] = [];
  for (const file of files) {
    const parts = file.name.split('/').filter(Boolean);
    let siblings = roots;
    let parentPath = '';
    parts.forEach((name, index) => {
      const path = parentPath ? `${parentPath}/${name}` : name;
      const kind = index === parts.length - 1 ? 'file' : 'folder';
      let node = siblings.find((candidate) => candidate.name === name && candidate.kind === kind);
      if (!node) {
        node = { name, path, kind, children: [], ...(kind === 'file' ? { file } : {}) };
        siblings.push(node);
      }
      siblings = node.children;
      parentPath = path;
    });
  }
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'folder' ? -1 : 1;
      if (left.path.toLocaleLowerCase() === 'readme.md') return -1;
      if (right.path.toLocaleLowerCase() === 'readme.md') return 1;
      return left.name.localeCompare(right.name);
    });
    nodes.forEach((node) => sort(node.children));
  };
  sort(roots);
  return roots;
}

function folders(nodes: TreeNode[]): string[] {
  return nodes.flatMap((node) => node.kind === 'folder'
    ? [node.path, ...folders(node.children)]
    : []);
}

function descendantFiles(node: TreeNode): KbFile[] {
  if (node.file) return [node.file];
  return node.children.flatMap(descendantFiles);
}

function visibleItems(
  nodes: TreeNode[],
  expanded: Set<string>,
  depth = 0,
  parentPath: string | null = null,
): VisibleItem[] {
  return nodes.flatMap((node) => [
    { node, depth, parentPath },
    ...(node.kind === 'folder' && expanded.has(node.path)
      ? visibleItems(node.children, expanded, depth + 1, node.path)
      : []),
  ]);
}

export function KnowledgeSourceExplorer({
  kbId,
  files,
  canUpdate,
  uploading,
  deleting,
  onUpload,
  onDeleteFiles,
  onDelete,
}: {
  kbId: string;
  files: KbFile[];
  canUpdate: boolean;
  uploading: boolean;
  deleting: boolean;
  onUpload: (items: Array<{ file: File; path: string }>) => void;
  onDeleteFiles: (files: KbFile[], folderPath: string) => void;
  onDelete: (file: KbFile) => void;
}) {
  const { t } = useTranslation();
  const pane = usePersistedPaneWidth({
    storageKey: `vibecanvas:knowledge-source-tree-width:v1:${kbId}`,
    defaultWidth: 272,
    minWidth: 220,
    maxWidth: 400,
  });
  const tree = useMemo(() => buildTree(files), [files]);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(folders(tree)));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = files.find((file) => file.id === selectedId)
    ?? files.find((file) => file.name.toLocaleLowerCase() === 'readme.md')
    ?? files[0]
    ?? null;
  const rows = useMemo(() => visibleItems(tree, expanded), [expanded, tree]);
  const refs = useRef(new Map<string, HTMLButtonElement>());
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [uploadPrefix, setUploadPrefix] = useState('');

  const toggle = (path: string) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });
  const focusAt = (index: number) => {
    const row = rows[index];
    if (row) refs.current.get(row.node.path)?.focus();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, row: VisibleItem) => {
    const index = rows.findIndex(({ node }) => node.path === row.node.path);
    if (event.key === 'ArrowDown') {
      event.preventDefault(); focusAt(Math.min(rows.length - 1, index + 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault(); focusAt(Math.max(0, index - 1));
    } else if (event.key === 'ArrowRight' && row.node.kind === 'folder') {
      event.preventDefault();
      if (!expanded.has(row.node.path)) toggle(row.node.path);
      else if (row.node.children[0]) refs.current.get(row.node.children[0].path)?.focus();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (row.node.kind === 'folder' && expanded.has(row.node.path)) toggle(row.node.path);
      else if (row.parentPath) refs.current.get(row.parentPath)?.focus();
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (row.node.kind === 'folder') toggle(row.node.path);
      else if (row.node.file) setSelectedId(row.node.file.id);
    }
  };
  const openFiles = (prefix: string) => {
    setUploadPrefix(prefix);
    fileInput.current?.click();
  };
  const openFolder = (prefix: string) => {
    setUploadPrefix(prefix);
    folderInput.current?.click();
  };
  const withPrefix = (path: string) => uploadPrefix ? `${uploadPrefix}/${path}` : path;

  const uploadMenu = (prefix: string, context: 'root' | 'folder') => (
    <>
      <ContextMenuItem disabled={uploading} onSelect={() => openFiles(prefix)}>
        <Upload className="mr-2 size-4" />
        {context === 'root' ? t('knowledge.uploadFiles', 'Upload files') : t('knowledge.uploadFilesHere', 'Upload files here')}
      </ContextMenuItem>
      <ContextMenuItem disabled={uploading} onSelect={() => openFolder(prefix)}>
        <FolderUp className="mr-2 size-4" />
        {context === 'root' ? t('knowledge.uploadFolder', 'Upload folder') : t('knowledge.uploadFolderHere', 'Upload folder here')}
      </ContextMenuItem>
    </>
  );

  return (
    <div className="flex h-full min-h-[28rem] flex-col overflow-hidden rounded-lg border border-edge-subtle bg-background md:flex-row">
      <aside
        className="relative flex max-h-56 min-h-0 w-full shrink-0 flex-col border-b border-edge-structural bg-surface-nav md:max-h-none md:w-[var(--knowledge-tree-width)] md:border-b-0 md:border-r"
        style={{ '--knowledge-tree-width': `${pane.width}px` } as CSSProperties}
      >
        <div className="hidden md:block">
          <PaneResizeHandle
            side="right"
            width={pane.width}
            minWidth={220}
            maxWidth={400}
            onWidthChange={pane.setWidth}
            onReset={pane.resetWidth}
            label={t('knowledge.resizeSources', 'Resize file tree')}
          />
        </div>
        <div className="flex h-10 shrink-0 items-center justify-between border-b px-3">
          <div className="flex min-w-0 items-center gap-2 text-xs font-medium">
            <FileTypeIcon directory className="size-6 rounded-md" />
            <span className="truncate">{t('knowledge.sourceFiles', 'Files')}</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="text-xs tabular-nums text-muted-foreground">{files.length}</span>
            {canUpdate ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon" className="size-7" aria-label={t('knowledge.fileActions', 'File actions')}>
                    <MoreHorizontal className="size-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem disabled={uploading} onSelect={() => openFiles('')}><Upload />{t('knowledge.uploadFiles', 'Upload files')}</DropdownMenuItem>
                  <DropdownMenuItem disabled={uploading} onSelect={() => openFolder('')}><FolderUp />{t('knowledge.uploadFolder', 'Upload folder')}</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
          </div>
        </div>
        <ContextMenu>
          <ContextMenuTrigger asChild>
            <div className="min-h-0 flex-1 overflow-auto py-1" role="tree" aria-label={t('knowledge.sourceFiles', 'Files')}>
              {rows.map((row) => {
                const { node } = row;
                const isSelected = node.file?.id === selected?.id;
                const rowButton = (
                  <button
                    ref={(element) => {
                      if (element) refs.current.set(node.path, element);
                      else refs.current.delete(node.path);
                    }}
                    type="button"
                    role="treeitem"
                    aria-expanded={node.kind === 'folder' ? expanded.has(node.path) : undefined}
                    aria-selected={node.kind === 'file' ? isSelected : undefined}
                    onClick={() => node.kind === 'folder' ? toggle(node.path) : node.file && setSelectedId(node.file.id)}
                    onKeyDown={(event) => onKeyDown(event, row)}
                    className={cn(
                      'flex h-8 w-full min-w-0 items-center gap-1.5 rounded-sm pr-2 text-left text-xs outline-none hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-ring',
                      isSelected && 'bg-surface-selected text-content-primary',
                    )}
                    style={{ paddingLeft: 8 + row.depth * 16 }}
                    title={node.path}
                  >
                    {node.kind === 'folder' ? (
                      <ChevronRight className={cn('size-3.5 shrink-0 transition-transform', expanded.has(node.path) && 'rotate-90')} />
                    ) : <span className="w-3.5 shrink-0" />}
                    <FileTypeIcon
                      fileName={node.name}
                      mimeType={node.file?.mime_type}
                      directory={node.kind === 'folder'}
                      open={node.kind === 'folder' && expanded.has(node.path)}
                      className="size-6 shrink-0 rounded-md"
                    />
                    <span className="min-w-0 flex-1 truncate">{node.name}</span>
                  </button>
                );
                if (!canUpdate) return <span key={`${node.kind}:${node.path}`} className="contents">{rowButton}</span>;
                return (
                  <ContextMenu key={`${node.kind}:${node.path}`}>
                    <ContextMenuTrigger asChild>{rowButton}</ContextMenuTrigger>
                    <ContextMenuContent>
                      {node.kind === 'folder' ? (
                        <>
                          {uploadMenu(node.path, 'folder')}
                          <ContextMenuSeparator />
                          <ContextMenuItem
                            className="text-destructive focus:text-destructive"
                            disabled={deleting}
                            onSelect={() => onDeleteFiles(descendantFiles(node), node.path)}
                          >
                            <Trash2 className="mr-2 size-4" />{t('knowledge.deleteFolder', 'Delete folder')}
                          </ContextMenuItem>
                        </>
                      ) : (
                        <ContextMenuItem
                          className="text-destructive focus:text-destructive"
                          disabled={deleting || node.file?.name.toLocaleLowerCase() === 'readme.md'}
                          onSelect={() => node.file && onDelete(node.file)}
                        >
                          <Trash2 className="mr-2 size-4" />{t('delete', 'Delete')}
                        </ContextMenuItem>
                      )}
                    </ContextMenuContent>
                  </ContextMenu>
                );
              })}
            </div>
          </ContextMenuTrigger>
          {canUpdate ? <ContextMenuContent>{uploadMenu('', 'root')}</ContextMenuContent> : null}
        </ContextMenu>
        <input
          ref={fileInput}
          type="file"
          multiple
          className="hidden"
          data-testid="knowledge-files-input"
          aria-label={t('knowledge.uploadFiles', 'Upload files')}
          onChange={(event) => {
            const picked = Array.from(event.currentTarget.files ?? []).map((file) => ({ file, path: withPrefix(file.name) }));
            if (picked.length) onUpload(picked);
            event.currentTarget.value = '';
          }}
        />
        <input
          ref={(element) => {
            folderInput.current = element;
            element?.setAttribute('webkitdirectory', '');
          }}
          type="file"
          multiple
          className="hidden"
          data-testid="knowledge-folder-input"
          aria-label={t('knowledge.uploadFolder', 'Upload folder')}
          onChange={(event) => {
            const picked = Array.from(event.currentTarget.files ?? []).map((file) => ({
              file,
              path: withPrefix(file.webkitRelativePath || file.name),
            }));
            if (picked.length) onUpload(picked);
            event.currentTarget.value = '';
          }}
        />
      </aside>

      <section className="min-h-96 min-w-0 flex-1 md:min-h-0" aria-live="polite">
        {selected ? (
          <KnowledgeFilePreview kbId={kbId} file={selected} />
        ) : null}
      </section>
    </div>
  );
}
