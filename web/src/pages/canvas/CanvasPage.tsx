/**
 * Workflow canvas: /workflow/:id resolves the active major once;
 * /version/vN follows that major's tip, and historical /version/vN.svM
 * snapshots stay fixed. A latest exact snapshot resolves to its major route.
 * Remote updates preserve dirty drafts and offer explicit reconciliation.
 */
import { useCallback, useEffect, useRef } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { ReactFlowProvider } from '@xyflow/react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { CanvasNodePicker } from './CanvasNodePicker';
import { Canvas } from '@/pages/canvas/Canvas';
import { decideSeed, showConflictToast } from '@/pages/canvas/seedPolicy';
import { useDirtyNavigationGuard } from '@/lib/navigation/use-dirty-navigation-guard';
import { UnsavedChangesDialog } from '@/pages/canvas/UnsavedChangesDialog';
import { useCommitWorkflow } from '@/lib/api/mutations/workflow-ops';
import { isAuthorizationChangedError } from '@/lib/api/mutations/error-message';
import { CanvasToolbar } from '@/pages/canvas/CanvasToolbar';
import { ContextMenuLayer } from '@/pages/canvas/ContextMenuLayer';
import { VersionBanner } from '@/pages/canvas/VersionBanner';
import { RightInspector } from '@/pages/canvas/inspector/RightInspector';
import { useWorkflow, useWorkflowAt, useWorkflowHead } from '@/lib/api/queries/workflow';
import { useWorkflowEditStore, stripWorkflowMeta } from '@/stores/workflow-edit';
import { useUIStore } from '@/stores/ui';
import { useExecStreamStore } from '@/stores/exec-stream';
import { AsyncState } from '@/components/ui/async-state';
import { WorkflowWorkbenchHeader } from './WorkflowWorkbenchHeader';
import { CanvasExecutionHistory } from './CanvasExecutionHistory';
import { useWorkflowExecutionStatus } from '@/lib/api/queries/executions';

export function CanvasPage() {
  const { wfId, vKey } = useParams<{ wfId: string; vKey?: string }>();

  // Parse the optional `v{N}.sv{M}` pin. A malformed vKey degrades to
  // latest rather than blowing up the route.
  const match = vKey?.match(/^v(\d+)\.sv(\d+)$/) ?? null;
  const followingMajor = vKey?.match(/^v([1-9]\d*)$/);
  const v = match ? Number(match[1]) : followingMajor ? Number(followingMajor[1]) : null;
  const sv = match ? Number(match[2]) : null;
  // `pinned` here = the PINNED-version source only. The query/seed logic keys
  // off this (a run must not disable the latest-snapshot query nor skip the
  // StartNode seed). It is NO LONGER an edit-gate — UX-5 makes a pinned
  // historical version EDITABLE (Save lands under that major via
  // `target_major`). The remaining edit gate is the in-flight run freeze
  // (`effectiveReadOnly` below).
  const isPinned = match !== null;
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const fixedSnapshot = searchParams.get("snapshot") === "1";
  const head = useWorkflowHead(wfId ?? '', v);
  const followsMajor = !!followingMajor;
  const resolvedRouteRef = useRef<string | null>(null);
  const readOnly = isPinned; // alias kept for the query/seed branches below
  // The historical major this route is pinned to (null when on the live
  // active workflow). Threaded to the toolbar so Save commits under it.
  const pinnedMajor = v;

  // Two queries, exactly one of which is enabled at a time. The disabled
  // one passes `''` for the wfId path-param so `enabled: !!wfId` gates it
  // off without us having to model `string | undefined` through the hook
  // signature.
  const latest = useWorkflow(readOnly || followsMajor ? '' : (wfId ?? ''));
  const snapshotSub = followsMajor ? (head.data?.sub ?? null) : sv;
  const pinned = useWorkflowAt(readOnly || followsMajor ? (wfId ?? '') : '', v, snapshotSub, followsMajor);
  const query = readOnly || followsMajor ? pinned : latest;

  // Resolve an opened latest snapshot once into a stable major route. Polling
  // that route never follows global HEAD when another major is edited.
  useEffect(() => {
    if (followsMajor) { resolvedRouteRef.current = null; return; }
    if (!head.data || !head.isFetchedAfterMount || fixedSnapshot) return;
    const route = `${wfId}:${vKey ?? ""}`;
    if (resolvedRouteRef.current === route) return;
    resolvedRouteRef.current = route;
    if (match && (head.data.major !== v || head.data.sub !== sv)) return;
    if (useWorkflowEditStore.getState().isDirty()) return;
    navigate(`/workflow/${wfId}/version/v${head.data.major}`, { replace: true });
  }, [head.data, head.isFetchedAfterMount, followsMajor, fixedSnapshot, v, sv, wfId, vKey, navigate]);

  const setDraft = useWorkflowEditStore((s) => s.setDraft);
  const applyServerMeta = useWorkflowEditStore((s) => s.applyServerMeta);
  // Subscribe to the dirty mirror so the guard re-derives on every edit.
  const dirty = useWorkflowEditStore((s) => s.dirty);
  // On a pinned historical route, the unsaved-guard "Save & continue" path must
  // also land under the pinned major (UX-5) — bind `target_major` here too.
  const commit = useCommitWorkflow(wfId ?? '', pinnedMajor);
  const setCanvasReadOnly = useUIStore((s) => s.setCanvasReadOnly);
  const setLastActiveWorkflowId = useUIStore((s) => s.setLastActiveWorkflowId);
  const setActiveChatId = useUIStore((s) => s.setActiveChatId);
  const resetExecStream = useExecStreamStore((s) => s.reset);
  // Freeze edits while a run is in flight. A pinned historical
  // version EDITABLE, so pinning NO LONGER contributes to the edit freeze — the
  // only remaining freeze is an active run. This single source is threaded as
  // the canvas/inspector/toolbar `readOnly` AND mirrored into `canvasReadOnly`
  // (window-level keyboard mutation gating).
  const localRunning = useExecStreamStore((s) => s.wfId === wfId && s.status === 'running');
  const workflowCapabilities = new Set(query.data?.meta.access?.capabilities ?? []);
  const canUpdate = workflowCapabilities.has('update');
  const canExecute = workflowCapabilities.has('execute');
  const canExport = workflowCapabilities.has('export');
  const canMount = workflowCapabilities.has('mount');
  const canInspectRuns = workflowCapabilities.has('inspect_runs');
  const canCancel = workflowCapabilities.has('cancel');
  const currentExecution = useWorkflowExecutionStatus(wfId, { enabled: canInspectRuns, running: localRunning });
  const isRunning = localRunning || currentExecution.data?.status === 'running';
  const effectiveReadOnly = isRunning || !canUpdate;
  // The Explorer itself now lives in AppLayout (B1 shell); CanvasPage only
  // keeps the toolbar's "Files" toggle wiring, which flips the same shared
  // `explorerOpen` store slice the AppLayout-level Explorer reads.
  const explorerOpen = useUIStore((s) => s.explorerOpen);
  const toggleExplorer = useUIStore((s) => s.toggleExplorer);
  const { t } = useTranslation();
  // Toast id is held in a ref so each conflicting refetch updates the same
  // banner instead of stacking duplicates.
  const conflictToastIdRef = useRef<string | number | null>(null);
  // Track the route identity (wfId + vKey) across renders so the seed effect
  // can distinguish a STRONG route/version navigation (hard re-seed) from a
  // same-route agent refetch (soft conflict guard) — Stream 0e.
  const routeKeyRef = useRef<string | null>(null);

  // The viewport-center getter (used by the Explorer palette's
  // double-click-to-insert) is now owned by `CanvasViewportProvider` in
  // `AppLayout` — a common ancestor of BOTH the canvas and the Explorer.
  // `Canvas` registers its getter there via `useRegisterViewportCenter`, so
  // CanvasPage no longer threads a `registerViewportCenter` prop.

  // Mirror the effective read-only flag (pinned or running) into the
  // UI store so window-level mutation entry points (keyboard undo/redo,
  // Stream 4 shortcuts) early-return on a pinned version AND during a run.
  // Single writer — no other owner writes `canvasReadOnly`.
  useEffect(() => {
    setCanvasReadOnly(effectiveReadOnly);
    return () => setCanvasReadOnly(false);
  }, [effectiveReadOnly, setCanvasReadOnly]);

  // Push the loaded snapshot into the edit store. We deliberately key the
  // effect on `data` reference: TanStack Query returns a stable reference
  // across re-renders until the query refetches, so this fires once per
  // load (and again on invalidation).
  //
  // Agent ↔ manual-edit policy (Stream 0e):
  //   - First load → always seed.
  //   - Route/version navigation (wfId/vKey changed) → STRONGER intent than
  //     an agent refetch → HARD re-seed (prompt if dirty — handled by the
  //     navigation guard; here we always seed the new route).
  //   - Same-route refetch + clean draft → safe re-seed (save echo, idle
  //     agent edit, invalidation).
  //   - Same-route refetch + dirty draft:
  //       * if the server bytes match our draft (a server echo of the
  //         user's OWN just-saved commit) → reconcile as clean, NO toast.
  //       * else if the server's COMMITTED GRAPH still equals the baseline
  //         graph (`__meta__` stripped) → no agent committed; this refetch
  //         only carries new `__meta__` (e.g. the user's own rename). Merge
  //         the new meta into draft + baseline, keep the unsaved edits, NO
  //         toast (Bug A).
  //       * otherwise (an agent committed a genuinely different graph) → keep
  //         the draft and show an ACTIONABLE conflict toast.
  // We read the store imperatively so the guard runs at effect time.
  useEffect(() => {
    if (!query.data || query.isPlaceholderData) return;
    const routeKey = `${wfId ?? ''}::${vKey ?? ''}`;
    const isNavigation = routeKeyRef.current !== routeKey;
    routeKeyRef.current = routeKey;

    const { draft, baseline } = useWorkflowEditStore.getState();
    const dirty = useWorkflowEditStore.getState().isDirty();
    const serverWorkflow = query.data.workflow;
    // The graph may omit or contain historical __meta__; trust the selected
    // immutable route or the snapshot's server metadata, never editable JSON.
    const loadedVersion = v !== null && snapshotSub !== null
      ? `v${v}.sv${snapshotSub}`
      : `v${query.data.meta.active_v}.sv${query.data.meta.active_sv}`;

    // Bug A discriminator: compare the server's committed GRAPH to the
    // BASELINE graph, both with `__meta__` stripped. Equal ⇒ the committed
    // graph has NOT changed externally (no agent commit) — so a dirty
    // divergence is the user's own edits and this refetch is a benign
    // meta-only echo (a rename).
    let serverGraphEqualsBaselineGraph = false;
    try {
      const baselineWf = JSON.parse(baseline);
      if (baselineWf && typeof baselineWf === 'object') {
        serverGraphEqualsBaselineGraph =
          JSON.stringify(stripWorkflowMeta(serverWorkflow)) ===
          JSON.stringify(stripWorkflowMeta(baselineWf));
      }
    } catch {
      serverGraphEqualsBaselineGraph = false;
    }

    const decision = decideSeed({
      draftIsNull: draft === null,
      isNavigation,
      dirty,
      serverEqualsDraft: JSON.stringify(draft) === JSON.stringify(serverWorkflow),
      serverGraphEqualsBaselineGraph,
    });

    if (decision === 'seed') {
      setDraft(serverWorkflow, loadedVersion);
      if (conflictToastIdRef.current !== null) {
        toast.dismiss(conflictToastIdRef.current);
        conflictToastIdRef.current = null;
      }
      return;
    }

    if (decision === 'meta-merge') {
      // Benign meta-only refetch (e.g. the user's own rename): apply the new
      // `__meta__` to draft + baseline WITHOUT clobbering the user's unsaved
      // graph edits, and dismiss any stale conflict toast.
      applyServerMeta((serverWorkflow as Record<string, unknown>).__meta__);
      if (conflictToastIdRef.current !== null) {
        toast.dismiss(conflictToastIdRef.current);
        conflictToastIdRef.current = null;
      }
      return;
    }

    // A genuine agent-vs-manual conflict: keep the draft, offer an explicit
    // reconciliation (replaces the old Infinity-duration no-action toast).
    const dismiss = () => {
      if (conflictToastIdRef.current !== null) {
        toast.dismiss(conflictToastIdRef.current);
        conflictToastIdRef.current = null;
      }
    };
    conflictToastIdRef.current = showConflictToast({
      id: conflictToastIdRef.current ?? undefined,
      message: t(
        'agent_changed_while_dirty',
        'The agent changed this workflow while you had unsaved edits.',
      ),
      loadLabel: t('load_agent_version', 'Load agent version (discard my edits)'),
      keepLabel: t('keep_my_edits', 'Keep mine'),
      onLoadAgent: () => {
        setDraft(serverWorkflow, loadedVersion);
        dismiss();
      },
      onKeepMine: dismiss,
    });
  }, [query.data, query.isPlaceholderData, setDraft, applyServerMeta, t, wfId, vKey, v, snapshotSub]);

  // NOTE: no auto-seed of a StartNode (user decision). A brand-new workflow
  // arrives as `{}` and stays a BLANK canvas — the truly-0-nodes empty-state
  // overlay (W3-2) invites the user to add nodes (right-click / drag a node
  // card / ask the agent). They add their own StartNode. This keeps a fresh
  // workflow clean (no spurious "Unsaved changes" from a client-side seed).

  // Unsaved-changes guard (Stream 8 M5): block in-app navigation + browser
  // close while the draft diverges from the last saved baseline. The draft is
  // the only copy of unsaved work. Read-only pinned versions fork-on-save, so
  // they rarely go dirty — the guard is still safe there (gated purely on
  // derived dirty, not on readOnly).
  const blocker = useDirtyNavigationGuard(dirty);
  const proceedNavigation = useCallback(() => {
    if (blocker.state === 'blocked') blocker.proceed();
  }, [blocker]);
  const cancelNavigation = useCallback(() => {
    if (blocker.state === 'blocked') blocker.reset();
  }, [blocker]);
  const saveThenProceed = useCallback(() => {
    const { draft } = useWorkflowEditStore.getState();
    if (!draft) {
      proceedNavigation();
      return;
    }
    commit.mutate(draft, {
      onSuccess: () => proceedNavigation(),
      // On error the commit hook already toasts; keep the user on the page
      // (blocker stays 'blocked') so they don't lose the draft.
    });
  }, [commit, proceedNavigation]);

  // Remember the last opened workflow so the workspace can offer a
  // "continue where you left off" shortcut (used by T15 command palette).
  useEffect(() => {
    if (wfId) setLastActiveWorkflowId(wfId);
  }, [wfId, setLastActiveWorkflowId]);

  // VFS 2c: clear the selected chat when the workflow changes so the
  // Explorer's /memory scratch scope doesn't carry a previous workflow's
  // chat id (the chat sidebar never auto-selects). Downstream queries are
  // already wf_id-scoped — this only fixes the cosmetic stale-scope view.
  useEffect(() => {
    setActiveChatId('chat', null);
  }, [wfId, setActiveChatId]);

  // Clear the execution stream when the workflow changes and on
  // unmount, so stale per-node execution rings (green/red) from a finished run
  // — or another workflow's run — never linger on the canvas. The cleanup
  // covers leaving the canvas entirely (e.g. back to /workspace); the
  // change-deps cover navigating between workflows.
  useEffect(() => {
    resetExecStream();
    return () => resetExecStream();
  }, [wfId, resetExecStream]);

  if (query.isLoading || (followsMajor && head.isLoading)) {
    return (
      <AsyncState
        kind="loading"
        className="m-6 h-[calc(100%-3rem)] border-0"
        title={t('canvas.loading', 'Loading workflow…')}
      />
    );
  }

  if (query.isError || (followsMajor && head.isError)) {
    const loadError = query.error ?? head.error;
    const unavailable = isAuthorizationChangedError(loadError);
    return (
      <AsyncState
        kind="error"
        className="m-6 h-[calc(100%-3rem)]"
        title={unavailable
          ? t('canvas.resourceUnavailable', 'Workflow unavailable')
          : t('canvas.loadError', 'Failed to load workflow')}
        description={unavailable
          ? t(
              'canvas.resourceUnavailableDescription',
              'This workflow does not exist or you no longer have permission to view it.',
            )
          : t('canvas.loadErrorDescription', 'Check the connection and try loading this workflow again.')}
        technicalDetails={!unavailable && loadError instanceof Error ? loadError.message : undefined}
        technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
        actionLabel={t('retry', 'Retry')}
        onAction={() => { void head.refetch(); void query.refetch(); }}
      />
    );
  }

  // Hoist the xyflow store one level up via `ReactFlowProvider` so the
  // right inspector (a sibling of `<Canvas>`, not a descendant) can
  // call `useNodes()` and read the live selection. Without an outer
  // provider, `<Canvas>`'s implicit provider would scope the store
  // strictly to its subtree and the inspector would crash.
  return (
    <ReactFlowProvider>
      <CanvasExecutionHistory wfId={wfId!} enabled={canInspectRuns}>
      <div
        className="flex h-full w-full flex-col"
        data-page-archetype="editor-workspace"
        data-edit-state={effectiveReadOnly ? 'view' : dirty ? 'dirty' : 'saved'}
      >
        <WorkflowWorkbenchHeader workflowId={wfId!} readOnlyName={isPinned} />
        {isPinned && vKey && <VersionBanner wfId={wfId!} vKey={vKey} />}
        <CanvasToolbar wfId={wfId!} readOnly={effectiveReadOnly}
          canExecute={canExecute} canExport={canExport} canMount={canMount}
          canInspectRuns={canInspectRuns} canCancel={canCancel}
          pinnedMajor={isPinned ? pinnedMajor : null}
          onToggleExplorer={toggleExplorer} explorerOpen={explorerOpen} />
        <div className="flex flex-1 overflow-hidden">
          <div className="flex min-h-0 flex-1 flex-col">
            <CanvasNodePicker readOnly={effectiveReadOnly}>
              <ContextMenuLayer readOnly={effectiveReadOnly}>
                <Canvas readOnly={effectiveReadOnly} />
              </ContextMenuLayer>
            </CanvasNodePicker>
          </div>
          <RightInspector wfId={wfId!} readOnly={effectiveReadOnly} canExecute={canExecute} />
        </div>
      </div>
      <UnsavedChangesDialog
        open={blocker.state === 'blocked'}
        saving={commit.isPending}
        onDiscard={proceedNavigation}
        onCancel={cancelNavigation}
        onSave={saveThenProceed}
      />
      </CanvasExecutionHistory>
    </ReactFlowProvider>
  );
}
