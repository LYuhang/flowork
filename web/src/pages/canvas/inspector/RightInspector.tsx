/**
 * Right-side inspector panel for the canvas page.
 *
 * Contextual by selection scope. The tab set depends on what is
 * selected on the canvas:
 *   - NODE scope (a node is selected, `inspectorScope === 'auto'`):
 *       `Node` / `Run node` / `Info`.
 *   - WORKFLOW scope (nothing selected, OR an explicit `inspectorScope ===
 *     'workflow'` override from the toolbar): `Run` / `Batch`.
 * The old fixed 4-tab strip (Node/Execute/Execution/Info) is gone; the
 * standalone Execution tab is removed (its live per-node status lives on the
 * canvas breathing ring; its persisted results fold into the workflow `Run`
 * tab).
 *
 * The scope/tab state machine lives in `useUIStore`:
 *   - `inspectorScope`: `'auto'` (derive from xyflow selection) | `'workflow'`
 *     (toolbar override; the toolbar also DESELECTS the node so the two
 *     sources can't contradict). Selecting a node here flips an active
 *     `'workflow'` override back to `'auto'`.
 *   - `inspectorTab`: the requested tab. We keep PER-SCOPE last-tab memory
 *     locally and reconcile the requested tab against the valid set.
 *   - run-start auto-focus: on the idle→running EDGE we
 *     `requestInspectorTab('workflow', 'run')`.
 *
 * Layout: on desktop the inspector remains a resizable flex sibling of the
 * canvas. On narrow viewports it becomes a bottom sheet with compact and
 * expanded heights so the canvas stays usable. Open/closed is the shared
 * `useUIStore.inspectorOpen` slice (#12): the toolbar button and the in-panel
 * close/collapse controls update the same flag. When closed the inspector
 * renders no visible panel (only the always-mounted Check dialog), so the
 * canvas reclaims the available area.
 */
import { useEffect, useRef, useState } from 'react';
import { ChevronRight, Maximize2, Minimize2, X } from 'lucide-react';
import { useNodes } from '@xyflow/react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/tabs';
import { NodeTab } from '@/pages/canvas/inspector/NodeTab';
import { NodeExecutePanel } from '@/pages/canvas/inspector/NodeExecutePanel';
import { InfoTab } from '@/pages/canvas/inspector/InfoTab';
import { WorkflowRunTab } from '@/pages/canvas/inspector/WorkflowRunTab';
import { BatchTab } from '@/pages/canvas/inspector/BatchTab';
import { WorkflowCheckDialog } from '@/components/modals/WorkflowCheckDialog';
import { useUIStore } from '@/stores/ui';
import { useExecStreamStore } from '@/stores/exec-stream';
import { PaneResizeHandle } from '@/components/ui/pane-resize-handle';
import { usePersistedPaneWidth } from '@/components/ui/use-persisted-pane-width';

export interface RightInspectorProps {
  wfId: string;
  /**
   * True for a pinned historical version or an in-flight run;
   * derived OR), the Node tab renders editing inputs disabled. The Run /
   * Batch / Info tabs are pure read/run views and are unaffected.
   */
  readOnly?: boolean;
  /** Exact backend-computed workflow execute capability. */
  canExecute?: boolean;
  variant?: 'default' | 'embedded';
}

type Scope = 'node' | 'workflow';
const NODE_TABS = ['node', 'run-node', 'info'] as const;
const WORKFLOW_TABS = ['run', 'batch'] as const;

export function RightInspector({ wfId, readOnly = false, canExecute = false, variant = 'default' }: RightInspectorProps) {
  const { t } = useTranslation();
  const { width, setWidth, resetWidth } = usePersistedPaneWidth({
    storageKey: `vibecanvas:workflow-inspector-width:v1:${wfId}`,
    defaultWidth: 380,
    minWidth: 320,
    maxWidth: 560,
  });
  // #12: open/collapse is a shared UI slice so the toolbar toggle button and the
  // in-panel collapse chevron flip the SAME source of truth. When closed the
  // inspector renders nothing and the canvas reclaims the space.
  const open = useUIStore((s) => s.inspectorOpen);
  const setInspectorOpen = useUIStore((s) => s.setInspectorOpen);
  const [mobileExpanded, setMobileExpanded] = useState(false);

  // ── Scope resolution ───────────────────────────────────────────────────
  const nodes = useNodes();
  const hasSelectedNode = nodes.some((n) => n.selected);
  const inspectorScope = useUIStore((s) => s.inspectorScope);
  const inspectorTab = useUIStore((s) => s.inspectorTab);
  const requestInspectorTab = useUIStore((s) => s.requestInspectorTab);
  // A node selected always wins UNLESS an explicit workflow override is set.
  const scope: Scope =
    inspectorScope === 'workflow' || !hasSelectedNode ? 'workflow' : 'node';

  // Selecting a node clears a stale `'workflow'` override → node-scope tabs.
  useEffect(() => {
    if (hasSelectedNode && inspectorScope === 'workflow') {
      requestInspectorTab('auto', 'node');
    }
  }, [hasSelectedNode, inspectorScope, requestInspectorTab]);

  // ── Per-scope last-tab memory ──────────────────────────────────────────
  const [nodeTab, setNodeTab] = useState<string>(() => {
    const stored = sessionStorage.getItem(`vibecanvas:inspector-node-tab:v1:${wfId}`);
    return stored && (NODE_TABS as readonly string[]).includes(stored) ? stored : 'node';
  });
  const [workflowTab, setWorkflowTab] = useState<string>(() => {
    const stored = sessionStorage.getItem(`vibecanvas:inspector-workflow-tab:v1:${wfId}`);
    return stored && (WORKFLOW_TABS as readonly string[]).includes(stored) ? stored : 'run';
  });

  useEffect(() => {
    sessionStorage.setItem(`vibecanvas:inspector-node-tab:v1:${wfId}`, nodeTab);
  }, [nodeTab, wfId]);
  useEffect(() => {
    sessionStorage.setItem(`vibecanvas:inspector-workflow-tab:v1:${wfId}`, workflowTab);
  }, [wfId, workflowTab]);

  // Reconcile an external `requestInspectorTab` into the right per-scope slot.
  useEffect(() => {
    queueMicrotask(() => {
      if ((NODE_TABS as readonly string[]).includes(inspectorTab)) {
        setNodeTab(inspectorTab);
      } else if ((WORKFLOW_TABS as readonly string[]).includes(inspectorTab)) {
        setWorkflowTab(inspectorTab);
      }
    });
  }, [inspectorTab]);

  const activeTab = scope === 'node'
    ? (!canExecute && nodeTab === 'run-node' ? 'node' : nodeTab)
    : workflowTab;
  const onTabChange = scope === 'node' ? setNodeTab : setWorkflowTab;

  // ── Run-start auto-focus (replaces the old single auto-switch effect) ───
  const execStatus = useExecStreamStore((s) => s.status);
  const prevExecStatus = useRef(execStatus);
  useEffect(() => {
    // Only on the idle/terminal → running EDGE (a fresh WORKFLOW run starting).
    if (execStatus === 'running' && prevExecStatus.current !== 'running') {
      requestInspectorTab('workflow', 'run');
    }
    prevExecStatus.current = execStatus;
  }, [execStatus, requestInspectorTab]);

  // ── Check dialog, opened through `requestCheck()` ───────────────────────
  const checkRequestId = useUIStore((s) => s.checkRequestId);
  const [checkOpen, setCheckOpen] = useState(false);
  const prevCheckId = useRef(checkRequestId);
  useEffect(() => {
    if (checkRequestId !== prevCheckId.current) {
      prevCheckId.current = checkRequestId;
      setCheckOpen(true);
    }
  }, [checkRequestId]);

  // The Check dialog is mounted EXACTLY ONCE below (outside the open/closed
  // panel), so toggling the inspector while a check is open never duplicates it
  // (the old code mounted it in BOTH the closed-early-return and the open
  // return → two dialogs / two closes). A `requestCheck()` fired while the panel
  // is hidden still opens it; the canvas reclaims the width when `!open`.
  return (
    <>
      {open && (
        <>
        <button
          type="button"
          className="fixed inset-0 z-auxiliary hidden bg-black/35 backdrop-blur-[1px] max-lg:block"
          aria-label={t('inspector.collapse', 'Collapse inspector')}
          onClick={() => setInspectorOpen(false)}
        />
        <div
          className={variant === 'embedded'
            ? `pane-enter-from-right relative flex shrink-0 select-text flex-col bg-surface-work max-lg:fixed max-lg:inset-x-0 max-lg:bottom-0 max-lg:z-modal max-lg:!w-full max-lg:rounded-t-2xl max-lg:border max-lg:border-b-0 max-lg:shadow-modal ${mobileExpanded ? 'max-lg:h-[min(92dvh,50rem)]' : 'max-lg:h-[min(56dvh,34rem)]'}`
            : `pane-enter-from-right surface-sidepanel relative flex shrink-0 select-text flex-col border-y-0 border-r-0 max-lg:fixed max-lg:inset-x-0 max-lg:bottom-0 max-lg:z-modal max-lg:!w-full max-lg:rounded-t-2xl max-lg:border max-lg:border-b-0 max-lg:shadow-modal ${mobileExpanded ? 'max-lg:h-[min(92dvh,50rem)]' : 'max-lg:h-[min(56dvh,34rem)]'}`}
          style={{ width }}
          role="dialog"
          aria-modal={false}
          aria-label={t('inspector.title', 'Inspector')}
        >
      <PaneResizeHandle
        side="left"
        width={width}
        minWidth={320}
        maxWidth={560}
        onWidthChange={setWidth}
        onReset={resetWidth}
        label={t('inspector.resize', 'Resize Inspector')}
        className="max-lg:hidden"
      />
      <div className="grid h-6 shrink-0 place-items-center rounded-t-2xl lg:hidden" aria-hidden="true">
        <span className="h-1 w-10 rounded-full bg-content-tertiary/45" />
      </div>
      <div className="flex h-12 shrink-0 items-center justify-between border-b px-3 max-lg:h-11">
        <div className="text-section">{t('inspector.title', 'Inspector')}</div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="toolbar-icon-button lg:hidden"
            aria-label={mobileExpanded
              ? t('inspector.compact', 'Use compact inspector height')
              : t('inspector.expand', 'Expand inspector')}
            title={mobileExpanded
              ? t('inspector.compact', 'Use compact inspector height')
              : t('inspector.expand', 'Expand inspector')}
            onClick={() => setMobileExpanded((expanded) => !expanded)}
          >
            {mobileExpanded ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="toolbar-icon-button"
            aria-label={t('inspector.collapse', 'Close inspector')}
            title={t('inspector.collapse', 'Close inspector')}
            data-action="inspector-collapse"
            onClick={() => setInspectorOpen(false)}
          >
            <X className="h-4 w-4 lg:hidden" />
            <ChevronRight className="hidden h-4 w-4 lg:block" />
          </Button>
        </div>
      </div>

      <Tabs
          value={activeTab}
          onValueChange={onTabChange}
          className="flex min-h-0 flex-1 flex-col"
        >
          {scope === 'node' ? (
            <TabsList variant="underline" className="h-10 w-full shrink-0 justify-start border-b border-edge-subtle px-3">
              <TabsTrigger value="node" data-testid="inspector-tab-node">
                {t('inspector.tab.node', 'Node')}
              </TabsTrigger>
              {canExecute ? (
                <TabsTrigger value="run-node" data-testid="inspector-tab-run-node">
                  {t('inspector.tab.runNode', 'Run node')}
                </TabsTrigger>
              ) : null}
              <TabsTrigger value="info" data-testid="inspector-tab-info">
                {t('inspector.tab.info', 'Info')}
              </TabsTrigger>
            </TabsList>
          ) : canExecute ? (
            <TabsList variant="underline" className="h-10 w-full shrink-0 justify-start border-b border-edge-subtle px-3">
              <TabsTrigger value="run" data-testid="inspector-tab-run">
                {t('inspector.tab.run', 'Run')}
              </TabsTrigger>
              <TabsTrigger value="batch" data-testid="inspector-tab-batch">
                {t('inspector.tab.batch', 'Batch')}
              </TabsTrigger>
            </TabsList>
          ) : (
            <div className="px-4 py-6 text-sm text-muted-foreground">
              {t('inspector.executeUnavailable', 'You can view this workflow, but running it is not included in your access.')}
            </div>
          )}

          {scope === 'node' ? (
            <>
              <TabsContent
                value="node"
                className="app-scrollbar min-h-0 flex-1 overflow-y-auto px-3 pb-3"
              >
                <NodeTab wfId={wfId} readOnly={readOnly} />
              </TabsContent>
              {/* forceMount: keep the result-heavy panel mounted-but-hidden so
                  switching tabs toggles visibility instead of rebuilding + re-
                  parsing a large result (Radix sets `hidden` when inactive). */}
              {canExecute ? (
                <TabsContent
                  forceMount
                  value="run-node"
                  className="app-scrollbar min-h-0 flex-1 overflow-y-auto px-3 pb-3 data-[state=inactive]:hidden"
                >
                  <NodeExecutePanel wfId={wfId} />
                </TabsContent>
              ) : null}
              <TabsContent
                value="info"
                className="app-scrollbar min-h-0 flex-1 overflow-y-auto px-3 pb-3"
              >
                <InfoTab wfId={wfId} />
              </TabsContent>
            </>
          ) : canExecute ? (
            <>
              {/* forceMount both: switching Run↔Batch toggles visibility
                  instead of unmounting/rebuilding (the heavy result re-parse +
                  re-layout that caused the lag). Radix hides the inactive one. */}
              <TabsContent
                forceMount
                value="run"
                className="app-scrollbar min-h-0 flex-1 overflow-y-auto px-3 pb-3 data-[state=inactive]:hidden"
              >
                <WorkflowRunTab wfId={wfId} allowResume />
              </TabsContent>
              <TabsContent
                forceMount
                value="batch"
                className="app-scrollbar min-h-0 flex-1 overflow-y-auto px-3 pb-3 data-[state=inactive]:hidden"
              >
                <BatchTab wfId={wfId} active={activeTab === 'batch'} />
              </TabsContent>
            </>
          ) : null}
      </Tabs>

        </div>
        </>
      )}
      <WorkflowCheckDialog open={checkOpen} onOpenChange={setCheckOpen} wfId={wfId} />
    </>
  );
}
