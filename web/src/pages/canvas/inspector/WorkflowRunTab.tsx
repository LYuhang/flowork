/**
 * Inspector workflow Run tab.
 *
 * Folds the retired `ExecuteInputDialog` modal into an inline tab:
 *
 *   1. INPUT — one row per StartNode input field (`getStartNodeFields` →
 *      {@link FieldValueWidget} with `allowReference={false}`: at the
 *      workflow-input boundary there is no upstream producer to reference,
 *      so the user supplies a preset value only). The UI keeps these values
 *      as raw editable strings; backend execution normalizes them against
 *      the StartNode field schema at the execution boundary.
 *
 *   2. RUN — starts the SSE execution itself (the trigger moved here from the
 *      toolbar/modal). `streamExecution` owns `begin()`, so the per-node
 *      cards light up + the canvas breathing ring fires exactly as before.
 *      Disabled while a run is in-flight (one run at a time; the toolbar's
 *      Cancel stops it).
 *
 *   3. OUTPUT — hydrates the latest process-local workflow execution state
 *      by wfId when no live stream is active. Empty state: the input form
 *      ALWAYS renders; the output region renders NOTHING until a run exists.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Play, Square } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { FieldValueWidget } from '@/pages/canvas/inspector/FieldValueWidget';
import { normalizeFieldType } from '@/pages/canvas/inspector/field-value-model';
import { getStartNodeFields } from '@/lib/workflow/start-node';
import { nodeLabel } from '@/lib/workflow/node-label';
import { streamExecution } from '@/lib/api/sse/exec-stream';
import { saveBeforeRun } from '@/lib/workflow/save-before-run';
import { useCommitWorkflow } from '@/lib/api/mutations/workflow-ops';
import type { components } from '@/lib/api/schema';
import { errorMessage } from '@/lib/api/mutations/error-message';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useWorkflowExecutionStatus } from '@/lib/api/queries/executions';
import { useRunWorkflowInputs } from '@/lib/api/queries/vfs';
import { cancelWorkflowExecution } from '@/lib/api/executions';

type WorkflowDraft = components['schemas']['CommitRequest']['workflow'];

interface NodeRow {
  status: string;
  result?: string;
  error?: string;
  /** Per-node wall-clock seconds — shown next to the node's status. */
  duration?: number;
}

/**
 * Reload-safe per-node output region. Live stream state wins while a workflow
 * run is active; otherwise the latest process-local workflow state is loaded
 * by wfId. When there is no run to show, render nothing.
 */
// Cap the characters laid out in each per-node result <pre>: many cards each
// rendering a multi-KB result is what janked the Run list.
const MAX_NODE_RESULT_CHARS = 20_000;

function RunOutput({ wfId }: { wfId: string }) {
  const { t } = useTranslation();
  const draft = useWorkflowEditStore((s) => s.draft);
  const storeWfId = useExecStreamStore((s) => s.wfId);
  const rawStatus = useExecStreamStore((s) => s.status);
  const perNode = useExecStreamStore((s) => s.perNode);
  const rawTotalDuration = useExecStreamStore((s) => s.totalDuration);

  // The live trio belongs to ONE workflow at a time (module singleton). If the
  // live run is for a different workflow, ignore it entirely here and fall back
  // to this workflow's process-local run state — execution state must not bleed
  // across workflows.
  const owns = storeWfId === wfId;
  const status = owns ? rawStatus : 'idle';
  const liveTotalDuration = owns ? rawTotalDuration : null;

  const liveEntries = owns ? Object.entries(perNode) : [];
  const isStreaming = status === 'running';
  const hasLive = liveEntries.length > 0;

  // Hydrate from the DB-backed current workflow execution once the live stream
  // is no longer running. Large node inputs/results are intentionally omitted
  // from live SSE frames, so terminal display should prefer the persisted state.
  const persisted = useWorkflowExecutionStatus(wfId, {
    enabled: !isStreaming,
  });

  // Reconcile to one rendered list. Live wins when present; otherwise map the
  // process-local `per_node` rows (`execution_result` → `result`). Memoized so a
  // re-render that didn't change perNode/persisted doesn't rebuild the array
  // (and re-render every node card with its potentially-large result).
  const rows = useMemo<Array<[string, NodeRow]>>(() => {
    const pn = persisted.data?.result ?? {};
    const persistedRows = Object.entries(pn).map(([nid, e]) => [
      nid,
      {
        status: e.status ?? 'unknown',
        result: e.execution_result,
        error: e.error,
        duration: e.duration,
      },
    ] satisfies [string, NodeRow]);
    if (!isStreaming && persistedRows.length > 0) {
      return persistedRows;
    }
    if (hasLive) {
      return (owns ? Object.entries(perNode) : []).map(([nid, e]) => [
        nid,
        { status: e.status, result: e.result, error: e.error, duration: e.duration },
      ]);
    }
    if (isStreaming) {
      return [];
    }
    return [];
  }, [hasLive, isStreaming, owns, perNode, persisted.data]);

  const displayStatus =
    status !== 'idle' ? status : (persisted.data?.status ?? 'idle');
  const completedCount = rows.filter(([, row]) => row.status === 'completed').length;
  const failedCount = rows.filter(([, row]) => row.status === 'failed' || !!row.error).length;

  // End-to-end time on the "Status:" line. Live wins (the terminal frame's
  // `duration`); otherwise derive it from the process-local record's
  // started/finished timestamps (the only total available post-reload).
  let totalDuration: number | null = liveTotalDuration;
  if (totalDuration == null && !hasLive) {
    const startedAt = persisted.data?.started_at;
    const finishedAt = persisted.data?.finished_at;
    if (typeof startedAt === 'number' && typeof finishedAt === 'number') {
      totalDuration = finishedAt - startedAt;
    }
  }

  // Render no output region until a run exists or a prior result is restored;
  // show a transient "Loading…" while actively recovering or hydrating
  // a known run — never a stale "No execution yet." card.
  if (isStreaming && !hasLive && rows.length === 0) {
    return (
      <div className="text-sm text-muted-foreground" data-testid="run-output">
        {t('inspector.run.running', 'Running…')}
      </div>
    );
  }

  if (displayStatus === 'idle' && rows.length === 0) {
    if (!hasLive && persisted.isLoading) {
      return (
        <p className="text-sm text-muted-foreground" data-testid="run-output-loading">
          {t('inspector.run.loading', 'Loading execution…')}
        </p>
      );
    }
    return null;
  }

  return (
    <div className="space-y-3" data-testid="run-output">
      <div className="text-ui">
        {t('inspector.run.status', 'Status:')}{' '}
        <span className="font-medium" data-testid="exec-status">
          {displayStatus}
        </span>
        {totalDuration != null && (
          <span className="text-muted-foreground" data-testid="exec-total-duration">
            {' '}
            · {totalDuration.toFixed(2)}s
          </span>
        )}
        {rows.length > 0 ? (
          <span className="text-muted-foreground">
            {' '}· {t('inspector.run.nodeSummary', '{{count}} nodes, {{completed}} completed, {{failed}} failed', {
              count: rows.length,
              completed: completedCount,
              failed: failedCount,
            })}
          </span>
        ) : null}
      </div>
      {rows.map(([nid, e]) => (
        <div
          key={nid}
          className="rounded-lg border bg-background p-2.5"
          data-testid="exec-node-card"
          data-node-id={nid}
        >
          <div className="text-meta font-medium">
            {nodeLabel(draft, nid)} —{' '}
            <span data-testid="exec-node-status">{e.status}</span>
            {e.duration != null && (
              <span className="text-muted-foreground" data-testid="exec-node-duration">
                {' '}
                · {e.duration.toFixed(2)}s
              </span>
            )}
          </div>
          {e.result && (
            <details className="mt-2 rounded border border-edge-subtle bg-surface-sunken/35">
              <summary className="cursor-pointer px-2 py-1.5 text-xs font-medium text-content-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus">
                {t('inspector.run.viewNodeOutput', 'View node output')}
              </summary>
              <pre className="app-scrollbar max-h-40 overflow-auto border-t border-edge-subtle p-2 text-code whitespace-pre-wrap break-words">
                {/* Cap the laid-out text — a huge per-node result janks the list;
                    the full value is in the Run-node panel + the /run file. */}
                {e.result.length > MAX_NODE_RESULT_CHARS
                  ? `${e.result.slice(0, MAX_NODE_RESULT_CHARS)}\n… (${t('inspector.run.truncated', 'truncated')})`
                  : e.result}
              </pre>
            </details>
          )}
          {e.error && (
            <pre className="app-scrollbar mt-1 max-h-40 overflow-auto rounded bg-destructive/10 p-2 text-code text-destructive whitespace-pre-wrap break-words">
              {e.error}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}

export interface WorkflowRunTabProps {
  wfId: string;
}

export function WorkflowRunTab({ wfId }: WorkflowRunTabProps) {
  const { t } = useTranslation();
  const draft = useWorkflowEditStore((s) => s.draft);
  const dirty = useWorkflowEditStore((s) => s.dirty);
  // `getStartNodeFields` returns a new array. Keeping that array unstable made
  // `fieldNames` change on every render; once persisted inputs existed, the
  // hydration effect below kept writing an equivalent `buffers` object and
  // caused a permanent render loop on the canvas.
  const fields = useMemo(() => getStartNodeFields(draft), [draft]);
  const fieldNames = useMemo(() => fields.map((f) => f.name), [fields]);
  const lastRunInputs = useRunWorkflowInputs(fieldNames.length > 0 ? wfId : null);

  // Save-if-dirty before executing: the engine loads the COMMITTED version by
  // wfId, so unsaved canvas edits would otherwise never run. `useCommitWorkflow`
  // toasts on save failure + re-baselines `dirty` on success.
  const commit = useCommitWorkflow(wfId);

  const execStatus = useExecStreamStore((s) => s.status);
  const execWfId = useExecStreamStore((s) => s.wfId);
  const isRunning = execWfId === wfId && execStatus === 'running';
  const startingRef = useRef(false);
  const [starting, setStarting] = useState(false);
  const isStarting = starting && !isRunning;

  // Per-field editing buffer: the raw (pre-coercion) literal the widget holds.
  // Seeded from the per-wfId persisted inputs so leaving + re-entering the
  // inspector restores what the user last ran (the result already survives via
  // the exec store; the inputs did not). Keyed by wfId so workflows don't bleed.
  const initialRememberedInputs = () =>
    useExecStreamStore.getState().inputsByWorkflow[wfId] ?? {};
  const [buffers, setBuffers] = useState<Record<string, unknown>>(
    initialRememberedInputs,
  );
  const hasRememberedInputsRef = useRef(
    Object.keys(initialRememberedInputs()).length > 0,
  );
  const userEditedRef = useRef(false);
  useEffect(() => {
    const remembered = initialRememberedInputs();
    hasRememberedInputsRef.current = Object.keys(remembered).length > 0;
    userEditedRef.current = false;
    queueMicrotask(() => setBuffers(remembered));
    // Reset only when switching workflows. Field changes within the same
    // workflow should not wipe values the user is editing in this panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wfId]);
  useEffect(() => {
    if (hasRememberedInputsRef.current || userEditedRef.current) return;
    if (!lastRunInputs) return;
    queueMicrotask(() => setBuffers((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const name of fieldNames) {
        if (Object.prototype.hasOwnProperty.call(lastRunInputs, name)) {
          const value = lastRunInputs[name];
          if (!Object.is(prev[name], value)) {
            next[name] = value;
            changed = true;
          }
        }
      }
      return changed ? next : prev;
    }));
  }, [fieldNames, lastRunInputs]);
  // Persist the buffer per wfId on every change so the next remount re-hydrates.
  useEffect(() => {
    useExecStreamStore.getState().setWorkflowInputs(wfId, buffers);
  }, [wfId, buffers]);
  const onRun = async () => {
    if (startingRef.current || isRunning || commit.isPending) return;
    const rawInput: Record<string, unknown> = {};
    for (const f of fields) {
      // An untouched boolean switch visually represents false.  Preserve that
      // exact value at the execution boundary instead of turning an absent OR
      // legacy-persisted empty buffer into '', which the workflow engine
      // correctly rejects as an invalid boolean.
      const buffered = buffers[f.name];
      rawInput[f.name] = normalizeFieldType(f.type) === 'boolean'
        && (buffered === undefined || buffered === null || buffered === '')
        ? false
        : (buffered ?? '');
    }

    startingRef.current = true;
    setStarting(true);
    const ac = new AbortController();
    useExecStreamStore.getState().begin(wfId, ac);
    // Save-if-dirty FIRST so the wfId-keyed execution runs the user's current
    // canvas, not the last-saved version. A clean draft skips the save (no
    // redundant identical subversion); a rejected save never starts the run.
    // `run` owns its own toast/status side-effects so save vs execution
    // failures stay cleanly separated (the commit mutation already toasts on
    // save failure — we don't want a second "Execution failed" toast).
    await saveBeforeRun({
      dirty: dirty && draft != null,
      draft,
      save: (wf) => commit.mutateAsync(wf as WorkflowDraft),
      run: async () => {
        try {
          await streamExecution({ wfId, input: rawInput, ac });
        } catch (e) {
          // AbortError fires when the toolbar Cancel calls `ac.abort()`; that
          // path already set status to 'cancelled'. Every other throw is real.
          if ((e as { name?: string }).name === 'AbortError') return;
          toast.error(
            t('inspector.run.executionFailed', 'Execution failed: {{msg}}', {
              msg: errorMessage(e),
            }),
          );
          useExecStreamStore.getState().setStatus('error');
        }
      },
    }).catch(() => {
      // Only a rejected SAVE reaches here (run swallows its own errors). The
      // commit mutation already surfaced "Save failed: …", so abort silently.
      useExecStreamStore.getState().reset();
    }).finally(() => {
      startingRef.current = false;
      setStarting(false);
    });
  };

  const onCancel = async () => {
    const { abortController, wfId: runningWfId } = useExecStreamStore.getState();
    if (runningWfId !== wfId) return;
    try {
      await cancelWorkflowExecution(wfId);
    } catch {
      // Best-effort server cancellation; still abort the local stream.
    }
    abortController?.abort();
    useExecStreamStore.getState().setStatus('cancelled');
  };

  return (
    <div className="space-y-4" data-testid="workflow-run-tab">
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-ui font-medium">{t('inspector.run.inputs', 'Inputs')}</h3>
          {fields.length > 0 && (
            <span className="text-meta">
              {t('inspector.run.inputCount', '{{count}} fields', { count: fields.length })}
            </span>
          )}
        </div>
        <div className="space-y-3">
        {fields.map((f) => {
          const idBase = `exec-input-${f.name}`;
          return (
            <div key={f.name} className="space-y-1" data-testid={`exec-field-${f.name}`}>
              <Label className="text-meta font-medium">
                {f.name}
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  ({f.type})
                </span>
              </Label>
              <FieldValueWidget
                type={f.type}
                value={buffers[f.name]}
                allowReference={false}
                deferCoercion
                idBase={idBase}
                onChange={(next) => {
                  userEditedRef.current = true;
                  setBuffers((b) => ({ ...b, [f.name]: next.value }));
                }}
              />
            </div>
          );
        })}
        </div>
      </section>

      <div className="sticky bottom-0 z-10 border-y border-edge-structural bg-surface-sidepanel/95 py-2 backdrop-blur">
        <Button
          className="w-full"
          data-action="run-workflow"
          variant={isRunning ? 'outline' : 'default'}
          disabled={commit.isPending || isStarting}
          onClick={() => void (isRunning ? onCancel() : onRun())}
        >
          {isStarting ? (
            t('inspector.run.starting', 'Starting...')
          ) : isRunning ? (
            <>
              <Square className="mr-2 h-4 w-4" />
              {t('cancel', 'Cancel')}
            </>
          ) : (
            <>
              <Play className="mr-2 h-4 w-4" />
              {t('execute', 'Execute')}
            </>
          )}
        </Button>
      </div>

      <section className="border-t border-edge-subtle pt-3">
        <h3 className="mb-3 text-ui font-medium">{t('inspector.run.output', 'Output')}</h3>
        <RunOutput wfId={wfId} />
      </section>
    </div>
  );
}
