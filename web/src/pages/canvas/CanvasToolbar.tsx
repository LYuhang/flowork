/**
 * Top toolbar for `CanvasPage`.
 *
 * Hosts the cross-cutting actions that operate on the *current* workflow:
 *   - Back  — navigate to `/workspace`.
 *   - Undo  — local in-memory undo from `useWorkflowEditStore`.
 *   - Redo  — local in-memory redo from `useWorkflowEditStore`.
 *   - Save  — commit the current draft.
 *   - Execute / Run Batch — open the inspector's run surfaces.
 *
 * Every actionable control carries a `data-action="<name>"` attribute so
 * the future T15 CommandPalette can dispatch synthetic clicks against the
 * same DOM (single source of truth: the button itself; the palette is a
 * pure dispatcher rather than a parallel handler set). Execute and Cancel
 * are rendered conditionally on the exec-stream `status`, but both keep
 * their `data-action` so the palette can target whichever is mounted.
 *
 * Undo/Redo intentionally drive only the local store's linear in-memory draft
 * history. Committed workflow versions are managed through the version UI.
 *
 * Execute flow:
 *   - The Execute / Run Batch buttons only open the Inspector's workflow Run or
 *     Batch tab. The actual Execute/Cancel control lives in that tab so the
 *     run form and its lifecycle are controlled from one place.
 */
import { useActiveHistoryExecution } from './ExecutionHistoryContext';
import { useCallback, useRef, useState } from 'react';
import {
  AlertCircle,
  Download,
  FolderTree,
  GitBranchPlus,
  Layers,
  LayoutGrid,
  MoreHorizontal,
  PanelRight,
  Play,
  Redo,
  Save,
  Settings,
  Undo,
  Upload,
} from 'lucide-react';
import { useReactFlow } from '@xyflow/react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { StatusDot, type SemanticStatus } from '@/components/ui/status';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { WorkflowSettingsModal } from '@/pages/canvas/WorkflowSettingsModal';
import { layoutWorkflowDict } from '@/pages/canvas/auto-layout';
import { errorMessage } from '@/lib/api/mutations/error-message';
import { useCommitWorkflow, useNewMajorVersion } from '@/lib/api/mutations/workflow-ops';
import { useWorkflow } from '@/lib/api/queries/workflow';
import {
  useCloseWorkflowSandbox,
  useStartWorkflowSandbox,
  useWorkflowSandboxStatus,
} from '@/lib/api/queries/workflow-sandbox';
import {
  downloadFilename,
  parseUploadedWorkflow,
  serializeWorkflow,
  WorkflowParseError,
} from '@/lib/workflow/io';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useUIStore } from '@/stores/ui';
import { formatSandboxTtl, sandboxTtlRemaining } from '@/lib/sandbox-status';

export interface CanvasToolbarProps {
  wfId: string;
  /**
   * When true (T14: pinned historical version), all mutating affordances
   * — Undo, Redo, Save, Execute/Cancel — are disabled. Only Check stays
   * enabled because it is a pure server-side read.
   *
   * Save is already gated by `dirty`, but a read-only canvas can become
   * dirty through programmatic edits (e.g., a stale `applyEdit` call),
   * so the explicit `readOnly` short-circuit is the belt-and-braces guard.
   */
  readOnly?: boolean;
  /** Exact backend-computed workflow execute capability. */
  canExecute?: boolean;
  /** Exact backend-computed workflow export capability. */
  canExport?: boolean;
  /** Exact backend-computed workflow mount capability. */
  canMount?: boolean;
  /** Exact backend-computed workflow run-inspection capability. */
  canInspectRuns?: boolean;
  /** Exact backend-computed workflow cancellation capability. */
  canCancel?: boolean;
  /**
   * UX-5: when the canvas is pinned to a HISTORICAL major (the `:vKey` route),
   * this is that major. Save then commits UNDER it (`target_major`) and the
   * page navigates to the new sub. `null`/undefined → editing the live active
   * workflow (Save advances the active major's sub, the legacy behaviour).
   */
  pinnedMajor?: number | null;
  /** VFS 2c: toggles the left Workflow Explorer pane. */
  onToggleExplorer: () => void;
  /** VFS 2c: current Explorer open state (drives the toggle's variant). */
  explorerOpen: boolean;
}

export function CanvasToolbar({
  wfId,
  readOnly = false,
  canExecute = false,
  canExport = false,
  canMount = false,
  canInspectRuns = false,
  canCancel = false,
  pinnedMajor = null,
  onToggleExplorer,
  explorerOpen,
}: CanvasToolbarProps) {
  const { t } = useTranslation();
  const draft = useWorkflowEditStore((s) => s.draft);
  const dirty = useWorkflowEditStore((s) => s.dirty);
  const undoStack = useWorkflowEditStore((s) => s.undoStack);
  const redoStack = useWorkflowEditStore((s) => s.redoStack);
  const undo = useWorkflowEditStore((s) => s.undo);
  const redo = useWorkflowEditStore((s) => s.redo);
  const applyEdit = useWorkflowEditStore((s) => s.applyEdit);

  const localRunning = useExecStreamStore((s) => s.wfId === wfId && s.status === 'running');
  const activeHistory = useActiveHistoryExecution(wfId);
  const isRunning = localRunning || activeHistory !== null;

  // Execute and Run Batch open the Inspector to the corresponding workflow tab;
  // run input and triggers live there. Check remains in the More menu.
  // Check fires via the shared `requestCheck` signal (it lives in the ⋯ menu,
  // which is unmounted until opened, so a DOM-click forward would no-op).
  const requestInspectorTab = useUIStore((s) => s.requestInspectorTab);
  const requestCheck = useUIStore((s) => s.requestCheck);
  // Right Inspector open/collapse is driven by this toolbar toggle (mirrors
  // the Explorer "Files" toggle). When closed RightInspector renders nothing
  // and the canvas reclaims the space.
  const inspectorOpen = useUIStore((s) => s.inspectorOpen);
  const toggleInspector = useUIStore((s) => s.toggleInspector);
  const setInspectorOpen = useUIStore((s) => s.setInspectorOpen);
  const { setNodes, getNodes } = useReactFlow();

  const navigate = useNavigate();
  // UX-5: Save commits under the pinned historical major when one is active
  // (its sv grows + HEAD moves onto it). `useCommitWorkflow` omits the field
  // when `pinnedMajor` is null, so live editing is unchanged.
  const commit = useCommitWorkflow(wfId, pinnedMajor);
  const newMajor = useNewMajorVersion(wfId);
  const { data: wfSnapshot } = useWorkflow(wfId);
  const sandbox = useWorkflowSandboxStatus(wfId, canInspectRuns);
  const startSandbox = useStartWorkflowSandbox(wfId);
  const closeSandbox = useCloseWorkflowSandbox(wfId);
  const [sandboxConfirmOpen, setSandboxConfirmOpen] = useState(false);
  const sandboxStatus = sandbox.data?.status ?? 'idle';
  const sandboxAllocated = [
    'running',
    'hibernating',
    'hibernated',
    'restoring',
    'releasing',
    'snapshot_failed',
  ].includes(sandboxStatus);
  const sandboxExecuting = (sandbox.data?.active_execution_ids?.length ?? 0) > 0;
  const sandboxBusy = startSandbox.isPending || closeSandbox.isPending;
  const sandboxRunning = sandboxStatus === 'running';
  const sandboxLabel = sandboxRunning
    ? sandboxExecuting
      ? t('workflow.sandbox.executing', 'Executing')
      : t('workflow.sandbox.running', 'Sandbox running')
    : sandboxStatus === 'hibernating'
      ? t('workflow.sandbox.hibernating', 'Creating snapshot')
    : sandboxStatus === 'restoring'
      ? t('workflow.sandbox.restoring', 'Restoring sandbox')
    : sandboxStatus === 'releasing'
      ? t('workflow.sandbox.releasing', 'Releasing sandbox')
    : sandboxStatus === 'hibernated'
      ? t('workflow.sandbox.hibernated', 'Sandbox hibernated')
    : sandboxStatus === 'snapshot_failed'
      ? t('workflow.sandbox.snapshot_failed', 'Snapshot failed')
    : sandboxStatus === 'closed'
      ? t('workflow.sandbox.closed', 'Sandbox closed')
      : t('workflow.sandbox.idle', 'Sandbox idle');
  const sandboxTone: SemanticStatus = sandboxRunning
    ? sandboxExecuting ? 'running' : 'success'
    : sandboxStatus === 'closed'
      ? 'danger'
      : 'neutral';
  const sandboxTtlLabel = sandboxAllocated
    ? formatSandboxTtl(sandboxTtlRemaining(sandbox.data))
    : null;
  const sandboxStatusLabel = sandbox.data?.ttl_paused && sandbox.data?.activity_state === 'busy'
    ? `${sandboxLabel} · ${t('workflow.sandbox.ttl_paused', 'TTL paused')}`
    : sandboxTtlLabel
    ? `${sandboxLabel} · ${sandboxTtlLabel}`
    : sandboxLabel;
  const toggleSandbox = () => {
    if (sandboxBusy) return;
    if (sandboxAllocated && canCancel) setSandboxConfirmOpen((open) => !open);
    else startSandbox.mutate();
  };

  // Deselect any selected node so xyflow selection (the scope source) can't
  // contradict the explicit `'workflow'` override the toolbar just set.
  const deselectAll = () =>
    setNodes((ns) => ns.map((n) => (n.selected ? { ...n, selected: false } : n)));

  const onToolbarExecute = () => {
    deselectAll();
    setInspectorOpen(true); // auto-open the Inspector to the Run tab
    requestInspectorTab('workflow', 'run');
  };
  const onToolbarRunBatch = () => {
    deselectAll();
    setInspectorOpen(true); // auto-open the Inspector to the Batch tab
    requestInspectorTab('workflow', 'batch');
  };

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [newVersionOpen, setNewVersionOpen] = useState(false);
  const [creatingVersion, setCreatingVersion] = useState(false);
  const creatingVersionRef = useRef(false);
  const settingsButtonRef = useRef<HTMLButtonElement>(null);
  const onSettingsOpenChange = useCallback((nextOpen: boolean) => {
    setSettingsOpen(nextOpen);
    if (!nextOpen) {
      requestAnimationFrame(() => settingsButtonRef.current?.focus());
    }
  }, []);
  // Stream 6 — confirm dialog gating a destructive JSON upload over a
  // non-empty / dirty canvas.
  const [pendingUpload, setPendingUpload] = useState<{ workflow: Record<string, unknown> } | null>(
    null,
  );
  const fileInputRef = useRef<HTMLInputElement>(null);

  const onSave = () => {
    if (!draft) return;
    if (pinnedMajor != null) {
      // Editing a HISTORICAL version: the commit lands under that major as a
      // new sub. Follow it to the just-saved vKey so the user keeps editing the
      // line they're on (and the pinned URL stays in sync with HEAD).
      commit.mutate(draft, {
        onSuccess: (meta) => {
          if (meta && meta.active_v != null && meta.active_sv != null) {
            navigate(`/workflow/${wfId}/version/v${meta.active_v}.sv${meta.active_sv}`);
          }
        },
      });
      return;
    }
    commit.mutate(draft);
  };

  // UX-5 (Part A): save-then-new-major. Only available on the LIVE workflow
  // (not a pinned historical version — major-versioning is meant for the
  // current canvas). Saves the draft first when dirty so the new major snapshots
  // the user's latest edits, then allocates a fresh major and lands the user on
  // the new active HEAD (`/workflow/:wfId`).
  const onNewVersion = async () => {
    if (!draft || pinnedMajor != null || readOnly || creatingVersionRef.current) return;
    creatingVersionRef.current = true;
    setCreatingVersion(true);
    try {
      if (dirty) await commit.mutateAsync(draft);
      await newMajor.mutateAsync(draft);
      setNewVersionOpen(false);
      navigate(`/workflow/${wfId}`);
      toast.success(t('toolbar.newVersionCreated', 'New version created'));
    } catch {
      // Both mutations toast on error via their onError handlers.
    } finally {
      creatingVersionRef.current = false;
      setCreatingVersion(false);
    }
  };

  // ----- Stream 6: JSON download / upload + Stream 8 auto-layout -----------

  // Download: serialize the current draft → a `.json` Blob + anchor click.
  // Filename from the workflow name + version (best-effort from the snapshot).
  const onDownload = () => {
    if (!draft) return;
    const json = serializeWorkflow(draft);
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const meta = wfSnapshot?.meta;
    const version =
      meta && meta.active_v != null ? `v${meta.active_v}.sv${meta.active_sv ?? 0}` : null;
    a.download = downloadFilename(meta?.workflow_name, version);
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  // Replace the live draft with the uploaded portable workflow content via one
  // applyEdit operation. Nodes and __meta__ (including code_requirements) round
  // trip together; unrelated top-level identity keys are filtered by the parser.
  const applyUpload = (uploaded: Record<string, unknown>) => {
    applyEdit((wf) => {
      // Wipe existing nodes, then apply uploaded nodes and meta. When importing
      // an older node-only file, preserve the current meta for compatibility.
      for (const key of Object.keys(wf)) {
        if (/^node_\d+$/.test(key)) delete wf[key];
      }
      for (const [key, value] of Object.entries(uploaded)) {
        if (/^node_\d+$/.test(key) || key === '__meta__') wf[key] = value;
      }
      // Lay out the freshly-loaded nodes that arrived without a position.
      layoutWorkflowDict(wf, { onlyPositionless: true });
      return wf;
    });
    toast.success(t('io.uploadLoaded', 'Workflow loaded onto the canvas.'));
  };

  // File picker → parse → validate. On a non-empty / dirty canvas, route through
  // a confirm dialog before clobbering; otherwise apply immediately.
  const onUploadFile = async (file: File) => {
    let parsed: { workflow: Record<string, unknown> };
    try {
      const text = await file.text();
      parsed = parseUploadedWorkflow(JSON.parse(text));
    } catch (e) {
      const msg = e instanceof WorkflowParseError ? e.message : errorMessage(e);
      toast.error(`${t('io.uploadFailed', 'Could not load workflow')}: ${msg}`);
      return;
    }
    const hasNodes =
      draft != null && Object.keys(draft).some((k) => /^node_\d+$/.test(k));
    if (hasNodes || dirty) {
      setPendingUpload(parsed);
    } else {
      applyUpload(parsed.workflow);
    }
  };

  const onUploadInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // Reset so re-selecting the same file fires `change` again.
    e.target.value = '';
    if (file) void onUploadFile(file);
  };

  // Stream 8 (N1): "Tidy up" — re-arrange every node via dagre, one undo step.
  const onAutoLayout = () => {
    if (!draft) return;
    applyEdit((wf) => layoutWorkflowDict(wf, { measuredNodes: getNodes() }));
  };

  return (
    <div className="surface-topbar app-scrollbar flex h-12 items-center gap-1.5 overflow-x-auto px-3">
      <div className="flex shrink-0 items-center rounded-md bg-surface-sunken p-0.5">
      <Button
        variant="ghost"
        size="icon"
        className="toolbar-icon-button"
        data-action="undo"
        aria-label={t('undo', 'Undo')}
        title={t('undo', 'Undo')}
        disabled={readOnly || undoStack.length === 0}
        onClick={undo}
      >
        <Undo className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="toolbar-icon-button"
        data-action="redo"
        aria-label={t('redo', 'Redo')}
        title={t('redo', 'Redo')}
        disabled={readOnly || redoStack.length === 0}
        onClick={redo}
      >
        <Redo className="h-4 w-4" />
      </Button>
      </div>
      <div className="ml-1 shrink-0">
        <button
          type="button"
          className="inline-flex h-8 shrink-0 items-center gap-2 rounded-md px-2 text-meta transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
          aria-label={sandboxAllocated
            ? t('workflow.sandbox.close', 'Close workflow sandbox')
            : t('workflow.sandbox.open', 'Open workflow sandbox')}
          aria-expanded={sandboxConfirmOpen}
          title={sandboxStatusLabel}
          disabled={sandboxBusy || (sandboxAllocated ? !canCancel : !canMount)}
          onClick={toggleSandbox}
        >
          <StatusDot status={sandboxTone} pulse={sandboxExecuting} />
          <span>{t('workflow.sandbox.title', 'Sandbox')}</span>
        </button>
      </div>
      <div className="flex-1" />
      <div className="mx-2 h-7 w-px shrink-0 bg-border" />
      <Button
        variant={explorerOpen ? 'secondary' : 'ghost'}
        size="icon"
        className="toolbar-icon-button"
        data-action="files"
        aria-label={t('vfs.toggle_explorer', 'Toggle file explorer')}
        title={t('vfs.toggle_explorer', 'Toggle file explorer')}
        onClick={onToggleExplorer}
      >
        <FolderTree className="h-4 w-4" />
      </Button>
      <Button
        variant={inspectorOpen ? 'secondary' : 'ghost'}
        size="icon"
        className="toolbar-icon-button"
        data-action="toggle-inspector"
        aria-label={t('inspector.toggle', 'Toggle inspector')}
        title={t('inspector.toggle', 'Toggle inspector')}
        onClick={toggleInspector}
      >
        <PanelRight className="h-4 w-4" />
      </Button>
      <Button
        data-action="canvas-save"
        variant="outline"
        className="h-9"
        disabled={readOnly || !dirty || commit.isPending}
        onClick={onSave}
      >
        <Save className="mr-2 h-4 w-4" />
        {t('save', 'Save')}
      </Button>
      {/* UX-5 (Part A): snapshot the current canvas into a fresh MAJOR version.
          Only on the LIVE workflow (hidden on a pinned historical route, where
          major-versioning the past doesn't make sense). */}
      {pinnedMajor == null && (
        <Button
          variant="outline"
          className="h-9 w-9 px-0 2xl:w-auto 2xl:px-4"
          data-action="canvas-new-version"
          disabled={readOnly || !draft || commit.isPending || newMajor.isPending}
          title={t('toolbar.newVersionHint', 'Save and start a new major version')}
          onClick={() => setNewVersionOpen(true)}
        >
          <GitBranchPlus className="h-4 w-4 2xl:mr-2" />
          <span className="hidden 2xl:inline">{t('toolbar.newVersion', 'New version')}</span>
        </Button>
      )}
      <Button
        variant="default"
        className="h-9"
        data-action="execute"
        disabled={!canExecute || isRunning}
        title={t('execute.openRunPanel', 'Open run panel')}
        onClick={onToolbarExecute}
      >
        <Play className="mr-2 h-4 w-4" />
        {t('execute', 'Execute')}
      </Button>
      <Button
        variant="outline"
        className="h-9 w-9 px-0 2xl:w-auto 2xl:px-4"
        data-action="canvas-run-batch"
        disabled={!canExecute}
        title={t('canvas.runBatch.openPanel', 'Open batch panel')}
        onClick={onToolbarRunBatch}
      >
        <Layers className="h-4 w-4 2xl:mr-2" />
        <span className="hidden 2xl:inline">{t('canvas.runBatch', 'Run Batch')}</span>
      </Button>
      <Button
        ref={settingsButtonRef}
        variant="ghost"
        size="icon"
        className="toolbar-icon-button"
        data-action="canvas-settings"
        aria-label={t('settings.workflow.title', 'Workflow settings')}
        title={t('settings.workflow.title', 'Workflow settings')}
        disabled={readOnly}
        onClick={() => onSettingsOpenChange(true)}
      >
        <Settings className="h-4 w-4" />
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="toolbar-icon-button"
            data-action="canvas-more"
            aria-label={t('canvas.moreActions', 'More actions')}
            title={t('canvas.moreActions', 'More actions')}
          >
            <MoreHorizontal className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-52">
          <DropdownMenuItem data-action="check" onSelect={() => requestCheck()}>
            <AlertCircle className="mr-2 h-4 w-4" />
            {t('check', 'Check')}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {canExport ? (
            <DropdownMenuItem data-action="wf-download" onSelect={onDownload}>
              <Download className="mr-2 h-4 w-4" />
              {t('io.download', 'Download JSON')}
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuItem
            data-action="wf-upload"
            disabled={readOnly}
            onSelect={(e) => {
              // The hidden <input> click must escape the menu's close/focus
              // teardown, so defer it a tick.
              e.preventDefault();
              setTimeout(() => fileInputRef.current?.click(), 0);
            }}
          >
            <Upload className="mr-2 h-4 w-4" />
            {t('io.upload', 'Upload JSON')}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            data-action="canvas-auto-layout"
            disabled={readOnly}
            onSelect={onAutoLayout}
          >
            <LayoutGrid className="mr-2 h-4 w-4" />
            {t('canvas.autoLayout', 'Tidy up')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        data-testid="wf-upload-input"
        onChange={onUploadInputChange}
      />
      <Dialog open={sandboxConfirmOpen && sandboxAllocated} onOpenChange={setSandboxConfirmOpen}>
        <DialogContent data-role="workflow-sandbox-close-confirm">
          <DialogHeader>
            <DialogTitle>{t('workflow.sandbox.confirmClose', 'Close workflow sandbox?')}</DialogTitle>
            <DialogDescription>
              {t('workflow.sandbox.confirmCloseDescription', 'Active sandbox resources for this workflow will be released.')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setSandboxConfirmOpen(false)}>
              {t('cancel', 'Cancel')}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={closeSandbox.isPending}
              onClick={() => {
                closeSandbox.mutate(undefined, {
                  onSettled: () => setSandboxConfirmOpen(false),
                });
              }}
            >
              {closeSandbox.isPending
                ? t('workflow.sandbox.closing', 'Closing…')
                : t('workflow.sandbox.closeAction', 'Close sandbox')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={pendingUpload !== null}
        onOpenChange={(o) => {
          if (!o) setPendingUpload(null);
        }}
      >
        <DialogContent data-testid="upload-confirm-dialog">
          <DialogHeader>
            <DialogTitle>{t('io.replaceTitle', 'Replace current canvas?')}</DialogTitle>
            <DialogDescription>
              {t('io.replaceBody', 'Unsaved changes will be lost.')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              data-action="wf-upload-cancel"
              onClick={() => setPendingUpload(null)}
            >
              {t('cancel', 'Cancel')}
            </Button>
            <Button
              variant="destructive"
              data-action="wf-upload-confirm"
              onClick={() => {
                if (pendingUpload) applyUpload(pendingUpload.workflow);
                setPendingUpload(null);
              }}
            >
              {t('io.replace', 'Replace')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={newVersionOpen} onOpenChange={(open) => { if (!creatingVersion) setNewVersionOpen(open); }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('toolbar.newVersionConfirmTitle')}</DialogTitle>
            <DialogDescription>{t('toolbar.newVersionConfirmBody')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" disabled={creatingVersion} onClick={() => setNewVersionOpen(false)}>
              {t('cancel', 'Cancel')}
            </Button>
            <Button data-action="canvas-new-version-confirm"
              disabled={readOnly || !draft || creatingVersion || commit.isPending || newMajor.isPending}
              onClick={() => void onNewVersion()}>
              {t(creatingVersion ? 'toolbar.newVersionCreating' : 'toolbar.newVersionConfirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <WorkflowSettingsModal open={settingsOpen} onOpenChange={onSettingsOpenChange} />
    </div>
  );
}
