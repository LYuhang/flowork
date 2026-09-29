import { useCallback, useEffect, useMemo, useRef, useState, type ComponentProps, type CSSProperties, type KeyboardEvent } from 'react';
import {
  ChevronRight,
} from 'lucide-react';
import { FileWorkbenchPreview } from '@/components/files/FileWorkbenchPreview';
import { PaneResizeHandle } from '@/components/ui/pane-resize-handle';
import { usePersistedPaneWidth } from '@/components/ui/use-persisted-pane-width';
import { cn } from '@/lib/utils';
import { FileTypeIcon } from '@/components/presentation/FileTypeIcon';

type LoadedFile =
  | { kind: 'ready'; blob: Blob }
  | { kind: 'binary' };

type FileTreeNode = {
  name: string;
  path: string;
  kind: 'folder' | 'file';
  children: FileTreeNode[];
};

function buildFileTree(paths: string[]): FileTreeNode[] {
  const roots: FileTreeNode[] = [];
  for (const rawPath of paths) {
    const parts = rawPath.split('/').filter(Boolean);
    let siblings = roots;
    let parentPath = '';
    parts.forEach((name, index) => {
      const path = parentPath ? `${parentPath}/${name}` : name;
      const kind = index === parts.length - 1 ? 'file' : 'folder';
      let node = siblings.find((candidate) => candidate.name === name && candidate.kind === kind);
      if (!node) {
        node = { name, path, kind, children: [] };
        siblings.push(node);
      }
      siblings = node.children;
      parentPath = path;
    });
  }

  const sortNodes = (nodes: FileTreeNode[]) => {
    nodes.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'folder' ? -1 : 1;
      if (left.path === 'SKILL.md') return -1;
      if (right.path === 'SKILL.md') return 1;
      return left.name.localeCompare(right.name);
    });
    nodes.forEach((node) => sortNodes(node.children));
  };
  sortNodes(roots);
  return roots;
}

function folderPaths(nodes: FileTreeNode[]): string[] {
  return nodes.flatMap((node) => node.kind === 'folder'
    ? [node.path, ...folderPaths(node.children)]
    : []);
}

type VisibleTreeItem = {
  node: FileTreeNode;
  depth: number;
  parentPath: string | null;
};

function visibleTreeItems(
  nodes: FileTreeNode[],
  expanded: Set<string>,
  depth = 0,
  parentPath: string | null = null,
): VisibleTreeItem[] {
  return nodes.flatMap((node) => [
    { node, depth, parentPath },
    ...(node.kind === 'folder' && expanded.has(node.path)
      ? visibleTreeItems(node.children, expanded, depth + 1, node.path)
      : []),
  ]);
}

function SkillFileBrowserContent({
  files,
  skillMd,
  loadFile,
  labels,
  persistKey = 'default',
  selectedPath,
  onSelectedPathChange,
}: {
  files: string[];
  skillMd: string;
  loadFile: (path: string) => Promise<Blob>;
  labels: { files: string; loading: string; failed: string; binary: string };
  persistKey?: string;
  selectedPath?: string;
  onSelectedPathChange?: (path: string) => void;
}) {
  const treePane = usePersistedPaneWidth({
    storageKey: `vibecanvas:skill-tree-width:v1:${persistKey}`,
    defaultWidth: 256,
    minWidth: 220,
    maxWidth: 380,
  });
  const orderedFiles = useMemo(
    () => ['SKILL.md', ...files.filter((path) => path !== 'SKILL.md')],
    [files],
  );
  const tree = useMemo(() => buildFileTree(orderedFiles), [orderedFiles]);
  const expansionStorageKey = `vibecanvas:skill-tree-expanded:v1:${persistKey}`;
  const [selected, setSelected] = useState(() =>
    selectedPath && orderedFiles.includes(selectedPath) ? selectedPath : 'SKILL.md',
  );
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try {
      const stored = sessionStorage.getItem(expansionStorageKey);
      return stored ? new Set(JSON.parse(stored) as string[]) : new Set(folderPaths(tree));
    } catch {
      return new Set(folderPaths(tree));
    }
  });
  const [loaded, setLoaded] = useState<LoadedFile>({ kind: 'binary' });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);
  const previousSelectedPathRef = useRef<string | undefined>(undefined);
  const cacheRef = useRef(new Map<string, LoadedFile>());
  const itemRefs = useRef(new Map<string, HTMLButtonElement>());
  const typeaheadRef = useRef({ value: '', timer: 0 });
  const [focusedPath, setFocusedPath] = useState(selected);

  const visibleItems = useMemo(
    () => visibleTreeItems(tree, expanded),
    [expanded, tree],
  );

  useEffect(() => {
    try {
      sessionStorage.setItem(expansionStorageKey, JSON.stringify([...expanded]));
    } catch {
      // Session persistence is a progressive enhancement.
    }
  }, [expanded, expansionStorageKey]);

  const selectFile = useCallback(async (path: string) => {
    const request = ++requestRef.current;
    // Record our intent before routing catches up. This prevents the previous
    // URL value from restoring the old row during the same render cycle, while
    // still allowing a genuine browser Back/Forward change to load below.
    previousSelectedPathRef.current = path;
    setSelected(path);
    setFocusedPath(path);
    onSelectedPathChange?.(path);
    setError(null);
    if (path === 'SKILL.md') {
      setLoading(false);
      return;
    }
    const cached = cacheRef.current.get(path);
    if (cached) {
      setLoaded(cached);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const blob = await loadFile(path);
      if (request !== requestRef.current) return;
      const next: LoadedFile = { kind: 'ready', blob };
      const cache = cacheRef.current;
      if (cache.size >= 50) {
        const oldestKey = cache.keys().next().value as string | undefined;
        if (oldestKey) cache.delete(oldestKey);
      }
      cache.set(path, next);
      setLoaded(next);
    } catch (reason) {
      if (request === requestRef.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, [loadFile, onSelectedPathChange]);

  useEffect(() => {
    if (selectedPath === previousSelectedPathRef.current) return;
    previousSelectedPathRef.current = selectedPath;
    if (!selectedPath || !orderedFiles.includes(selectedPath)) return;
    queueMicrotask(() => { void selectFile(selectedPath); });
  }, [orderedFiles, selectFile, selectedPath]);

  const toggleFolder = (path: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const focusItem = (path: string) => {
    setFocusedPath(path);
    itemRefs.current.get(path)?.focus();
  };

  const handleTreeKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    item: VisibleTreeItem,
  ) => {
    const index = visibleItems.findIndex(({ node }) => node.path === item.node.path);
    const focusAt = (nextIndex: number) => {
      const next = visibleItems[nextIndex];
      if (next) focusItem(next.node.path);
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
    } else if (event.key === 'ArrowRight' && item.node.kind === 'folder') {
      event.preventDefault();
      if (!expanded.has(item.node.path)) toggleFolder(item.node.path);
      else if (item.node.children[0]) focusItem(item.node.children[0].path);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (item.node.kind === 'folder' && expanded.has(item.node.path)) toggleFolder(item.node.path);
      else if (item.parentPath) focusItem(item.parentPath);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (item.node.kind === 'folder') toggleFolder(item.node.path);
      else void selectFile(item.node.path);
    } else if (event.key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey) {
      window.clearTimeout(typeaheadRef.current.timer);
      const query = `${typeaheadRef.current.value}${event.key}`.toLocaleLowerCase();
      typeaheadRef.current.value = query;
      typeaheadRef.current.timer = window.setTimeout(() => {
        typeaheadRef.current.value = '';
      }, 600);
      const ordered = [...visibleItems.slice(index + 1), ...visibleItems.slice(0, index + 1)];
      const match = ordered.find(({ node }) => node.name.toLocaleLowerCase().startsWith(query));
      if (match) focusItem(match.node.path);
    }
  };
  return (
    <div className="flex h-full min-h-[26rem] flex-col overflow-hidden rounded-md border border-edge-subtle bg-background md:flex-row">
      <aside
        className="relative flex max-h-56 min-h-0 w-full shrink-0 flex-col border-b border-edge-structural bg-surface-nav md:max-h-none md:w-[var(--skill-tree-width)] md:border-b-0 md:border-r"
        style={{ '--skill-tree-width': `${treePane.width}px` } as CSSProperties}
      >
        <div className="hidden md:block">
          <PaneResizeHandle
            side="right"
            width={treePane.width}
            minWidth={220}
            maxWidth={380}
            onWidthChange={treePane.setWidth}
            onReset={treePane.resetWidth}
            label={`${labels.files}: resize`}
          />
        </div>
        <div className="flex h-10 shrink-0 items-center justify-between border-b px-3">
          <div className="flex min-w-0 items-center gap-2 text-xs font-medium">
            <FileTypeIcon directory className="size-6 rounded-md" />
            <span className="truncate">{labels.files}</span>
          </div>
          <span className="text-xs tabular-nums text-muted-foreground">{orderedFiles.length}</span>
        </div>
        <div className="page-scroll-region flex-1 p-1.5" role="tree" aria-label={labels.files}>
          {visibleItems.map((item) => {
            const { node, depth } = item;
            const isFolder = node.kind === 'folder';
            const isExpanded = isFolder && expanded.has(node.path);
            return (
              <div
                key={`${node.kind}:${node.path}`}
                role="treeitem"
                aria-level={depth + 1}
                aria-expanded={isFolder ? isExpanded : undefined}
                aria-selected={!isFolder ? selected === node.path : undefined}
              >
                <button
                  ref={(element) => {
                    if (element) itemRefs.current.set(node.path, element);
                    else itemRefs.current.delete(node.path);
                  }}
                  type="button"
                  tabIndex={focusedPath === node.path ? 0 : -1}
                  onFocus={() => setFocusedPath(node.path)}
                  onKeyDown={(event) => handleTreeKeyDown(event, item)}
                  onClick={() => isFolder ? toggleFolder(node.path) : void selectFile(node.path)}
                  className={cn(
                    'interactive-row flex h-8 w-full items-center gap-1.5 rounded px-1.5 text-left text-xs',
                    !isFolder && selected === node.path
                      ? 'interactive-selected font-medium text-content-primary'
                      : 'text-content-secondary',
                  )}
                  style={{ paddingLeft: `${6 + depth * 14}px` }}
                  title={node.path}
                >
                  {isFolder ? (
                    <>
                      <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 transition-transform duration-feedback', isExpanded && 'rotate-90')} />
                      <FileTypeIcon
                        directory
                        open={isExpanded}
                        className="size-6 rounded-md"
                      />
                    </>
                  ) : (
                    <>
                      <span className="w-3.5 shrink-0" />
                      <FileTypeIcon fileName={node.path} className="size-6 rounded-md" />
                    </>
                  )}
                  <span className="truncate">{node.name}</span>
                </button>
              </div>
            );
          })}
        </div>
      </aside>

      <section className="min-h-96 min-w-0 flex-1 md:min-h-0">
        <FileWorkbenchPreview
          fileName={selected}
          mimeType={selected === 'SKILL.md' ? 'text/markdown' : loaded.kind === 'ready' ? loaded.blob.type : null}
          blob={selected !== 'SKILL.md' && loaded.kind === 'ready' ? loaded.blob : null}
          text={selected === 'SKILL.md' ? skillMd : null}
          loading={loading}
          error={error ? `${labels.failed}: ${error}` : null}
        />
      </section>
    </div>
  );
}

// File caches, pending requests and tree selection belong to one package revision.
export function SkillFileBrowser(props: ComponentProps<typeof SkillFileBrowserContent>) {
  return <SkillFileBrowserContent key={props.persistKey ?? 'default'} {...props} />;
}
