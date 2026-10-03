import { useEffect, useState } from 'react';
import { ChevronLeft, RefreshCw } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useUIStore } from '@/stores/ui';
import { PaneResizeHandle } from '@/components/ui/pane-resize-handle';
import { usePersistedPaneWidth } from '@/components/ui/use-persisted-pane-width';
import { useWorkflow } from '@/lib/api/queries/workflow';
import { WorkflowVersionsSection } from './WorkflowVersionsSection';
import { VfsFilesSection } from './VfsFilesSection';
import { VfsFileModal } from './VfsFileModal';
import { VfsRunFileModal } from './VfsRunFileModal';
import { VfsRunSection } from './VfsRunSection';
import { NodesSection } from './NodesSection';
import { ExplorerBlock } from './ExplorerBlock';
import { useWorkflowWorkspaceIdentity } from '@/lib/api/queries/workflow-workspace';

export interface VfsExplorerProps {
  /** Active workflow id, route-derived by AppLayout. */
  wfId: string;
  /** Whether the page is a pinned read-only version view (route `:vKey`). */
  readOnly: boolean;
  /** The pinned version key (`v{N}.sv{M}`) when viewing a historical version —
   *  used to mark THAT version's row as "current" in the Versions list. */
  vKey?: string;
}

/**
 * Left-rail Explorer. A top-level peer of the canvas + Agent (B1 shell),
 * mounted by AppLayout and gated on the workflow route. Reads its open/closed
 * state from the shared `explorerOpen` store slice (toggled by the
 * CanvasToolbar "Files" button) and self-fetches the active version pointer
 * via `useWorkflow(wfId)` — the SAME `['workflow', wfId]` query CanvasPage
 * uses, so it's TanStack-cached, not a double-fetch.
 */
export function VfsExplorer({ wfId, readOnly, vKey }: VfsExplorerProps) {
  const { t } = useTranslation();
  const { width, setWidth, resetWidth } = usePersistedPaneWidth({
    storageKey: `vibecanvas:workflow-explorer-width:v1:${wfId}`,
    defaultWidth: 288,
    minWidth: 240,
    maxWidth: 420,
  });
  const qc = useQueryClient();
  const open = useUIStore((s) => s.explorerOpen);
  const setExplorerOpen = useUIStore((s) => s.setExplorerOpen);
  const [file, setFile] = useState<{ scopeId: string; path: string } | null>(null);
  const [runFile, setRunFile] = useState<{ runId: string; path: string } | null>(null);
  const [sandboxSelectionKey, setSandboxSelectionKey] = useState<string | null>(null);
  useEffect(() => {
    queueMicrotask(() => setSandboxSelectionKey(null));
  }, [wfId]);

  // Self-fetch the version-tree HEAD so WorkflowVersionsSection can mark the
  // "current" row. Same query key as CanvasPage → cached (no extra request on
  // the live route; on a pinned `/version` route this fires the one request
  // CanvasPage suppressed, an accepted cost — "current" = live head, matching
  // today's behavior).
  const workflow = useWorkflow(wfId);
  const activeMajor = workflow.data?.meta?.active_v ?? null;
  const activeSub = workflow.data?.meta?.active_sv ?? null;
  const workspace = useWorkflowWorkspaceIdentity(wfId, open);
  const mountScopeId = workspace.data?.mount_scope_id ?? null;
  // When pinned to a historical version, the canvas is SHOWING that version, so
  // its major is what should read "current" in the list (not the HEAD). Parse
  // the major out of the `v{N}.sv{M}` key; null on the live route → HEAD marks.
  const viewedMajor = (() => {
    const m = vKey?.match(/^v(\d+)(?:\.sv\d+)?$/);
    return m ? Number(m[1]) : null;
  })();

  if (!open) return null;

  return (
    <div
      className="pane-enter-from-left relative flex shrink-0 flex-col bg-surface-work"
      style={{ width }}
    >
      <PaneResizeHandle
        side="right"
        width={width}
        minWidth={240}
        maxWidth={420}
        onWidthChange={setWidth}
        onReset={resetWidth}
        label={t('explorer.resize', 'Resize Explorer')}
      />
      <div className="flex h-12 items-center justify-between border-b border-edge-structural bg-surface-raised/90 px-3">
        <div className="text-sm font-semibold">{t('vfs.explorer_title', 'Explorer')}</div>
        <div className="flex items-center">
          <Button variant="ghost" size="icon" aria-label={t('vfs.refresh', 'Refresh')}
            onClick={() => qc.invalidateQueries({ queryKey: ['vfs'] })}>
            <RefreshCw className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" aria-label={t('vfs.collapse', 'Collapse explorer')}
            data-action="explorer-collapse" onClick={() => setExplorerOpen(false)}>
            <ChevronLeft className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <div className="flex-1 overflow-auto">
        <ExplorerBlock id="versions" title={t('vfs.versions', 'Workflow Versions')} persistenceKey={`vibecanvas:explorer-section:v1:${wfId}:versions`}>
          <WorkflowVersionsSection wfId={wfId} activeMajor={activeMajor} activeSub={activeSub} viewedMajor={viewedMajor} />
        </ExplorerBlock>
        <ExplorerBlock id="nodes" title={t('explorer.nodes', 'Nodes')} persistenceKey={`vibecanvas:explorer-section:v1:${wfId}:nodes`}>
          <NodesSection readOnly={readOnly} />
        </ExplorerBlock>
        <ExplorerBlock id="sandbox" title={t('vfs.sandbox', 'Sandbox')} defaultCollapsed={false} persistenceKey={`vibecanvas:explorer-section:v1:${wfId}:sandbox`}>
          <VfsRunSection
            wfId={wfId}
            onOpenFile={(path, runId) => setRunFile({ runId, path })}
          />
          {mountScopeId ? (
            <VfsFilesSection
              wfId={mountScopeId}
              open={open}
              roots={['mount']}
              selectionKey={sandboxSelectionKey}
              onSelectionKeyChange={setSandboxSelectionKey}
              onOpenFile={(path) => setFile({ scopeId: mountScopeId, path })}
            />
          ) : (
            <div className="px-3 py-2 text-[13px] text-muted-foreground">
              {workspace.isError
                ? t('vfs.files_error', 'Failed to load files.')
                : t('vfs.loading', 'Loading…')}
            </div>
          )}
        </ExplorerBlock>
      </div>
      <VfsFileModal wfId={file?.scopeId} path={file?.path ?? null} onClose={() => setFile(null)} />
      <VfsRunFileModal
        runId={runFile?.runId ?? null}
        path={runFile?.path ?? null}
        onClose={() => setRunFile(null)}
      />
    </div>
  );
}
