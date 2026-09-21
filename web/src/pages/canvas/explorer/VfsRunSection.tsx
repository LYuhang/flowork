import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useVfsRunList } from '@/lib/api/queries/vfs';
import { buildRunFileTree, type RunFileTreeNode } from './fileTree';
import { CollapsibleFolder } from './CollapsibleFolder';
import { VfsItemMenu } from './VfsItemMenu';
import { formatBytes } from '@/lib/format/bytes';
import { FileTypeIcon } from '@/components/presentation/FileTypeIcon';

export interface VfsRunSectionProps {
  /** Active workflow id. The workflow run tier is stable: run_id == wfId. */
  wfId: string;
  /** Open a run-tier file by its path and run id. */
  onOpenFile: (path: string, runId: string) => void;
}

/**
 * WORKFLOW_SANDBOX — the workflow's stable run-tier filesystem.
 *
 * Interactive workflow execution reuses `run_id == wfId` and clears the folder
 * before each run. The execution id is only the history row id; it is not the
 * VFS run id. Default-OPEN.
 */
export function VfsRunSection({ wfId, onOpenFile }: VfsRunSectionProps) {
  const { t } = useTranslation();
  const runId = wfId;
  // The top-level ``run`` folder is shown ALWAYS + expanded by default so the
  // workflow-sandbox structure is perceivable even before the first run.
  const [runOpen, setRunOpen] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const q = useVfsRunList(runId, { poll: true });

  const toggle = (path: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const indent = (depth: number) => ({ paddingLeft: (depth + 1) * 12 + 8 });

  const renderNode = (node: RunFileTreeNode, depth: number): React.ReactNode => {
    if (node.kind === 'file') {
      const e = node.entry!;
      return (
        // Run-tier files are read-only/ephemeral → Download + Copy Path only
        // Run files are read-only. Scope sign() by runId, not wfId.
        <VfsItemMenu
          key={node.path}
          path={e.path}
          name={node.name}
          isFolder={false}
          runId={runId}
          capabilities={e.capabilities}
        >
          <button
            type="button"
            aria-label={node.name}
            className="flex w-full items-center gap-2 rounded py-1 text-left text-[13px] hover:bg-muted"
            style={{ paddingLeft: depth * 12 + 8 }}
            onClick={() => onOpenFile(e.path, runId)}
            onDoubleClick={() => onOpenFile(e.path, runId)}
          >
            <FileTypeIcon
              fileName={node.name}
              mimeType={e.content_type}
              className="size-6 rounded-md"
            />
            <span className="truncate font-mono">{node.name}</span>
            <span className="rounded bg-muted px-1 text-xs text-muted-foreground">{e.content_type}</span>
            <span className="text-xs text-muted-foreground">{formatBytes(e.size_bytes)}</span>
          </button>
        </VfsItemMenu>
      );
    }
    const isOpen = expanded.has(node.path);
    return (
      <CollapsibleFolder key={node.path} label={node.name} depth={depth} open={isOpen} onToggle={() => toggle(node.path)}>
        {node.children.length === 0 ? (
          <div className="py-1 text-[13px] text-muted-foreground" style={indent(depth)}>
            {t('vfs.run_empty_folder', 'Empty.')}
          </div>
        ) : (
          node.children.map((c) => renderNode(c, depth + 1))
        )}
      </CollapsibleFolder>
    );
  };

  // The run-tier contents live UNDER a single ``run`` folder node that is ALWAYS
  // shown (even before a run), so the user perceives the workflow-sandbox
  // structure — mirroring how AGENT_SANDBOX always shows its fixed folders.
  // Default-OPEN. Inner states render at depth 1 (inside the folder).
  const body = () => {
    if (q.isLoading) {
      return (
        <div className="py-1 text-[13px] text-muted-foreground" style={indent(1)}>
          {t('vfs.loading', 'Loading…')}
        </div>
      );
    }
    if (q.isError) {
      return (
        <div className="py-1 text-[13px] text-destructive" style={indent(1)}>
          {t('vfs.run_error', 'Failed to load run files.')}
        </div>
      );
    }
    const tree = buildRunFileTree(q.data?.entries ?? []);
    // Run-tier entries are stored with a leading `/run` segment, so
    // buildRunFileTree yields a single top `run` folder. The wrapper
    // CollapsibleFolder below ALREADY is that run root — lift its children out so
    // the structure reads `/run/__exec__/…` and not a doubled `/run/run/__exec__`.
    const inner = tree.length === 1 && tree[0].name === 'run' ? tree[0].children : tree;
    if (inner.length === 0) {
      return (
        <div className="py-1 text-[13px] text-muted-foreground" style={indent(1)}>
          {t('vfs.run_no_files', 'This run produced no files.')}
        </div>
      );
    }
    return <>{inner.map((n) => renderNode(n, 1))}</>;
  };

  return (
    <div>
      <CollapsibleFolder
        label="run"
        depth={0}
        open={runOpen}
        onToggle={() => setRunOpen((v) => !v)}
      >
        {body()}
      </CollapsibleFolder>
    </div>
  );
}
