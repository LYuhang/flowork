import { ExecutionTraces } from './ExecutionTraces';
import { ResourceAuditPanel } from './ResourceAuditPanel';
/**
 * Inspector panel for debugging a single node.
 *
 * Click a node, open Execute, provide input, run it, stream logs, and allow
 * cancellation at any time. The Execute tab shows its input fields
 * (preset values only — no graph references, since a node-debug run is
 * isolated and `run_node` does NO reference resolution), Run kicks a
 * spinner, the output log fills with the result/error, and Stop aborts.
 *
 * State lives in the dedicated `useNodeExecStore` (NOT the workflow
 * whole-workflow execution store so a node-debug run never paints the canvas
 * as "running". One run at a time — Run is disabled while running (M4).
 *
 * The draft node_dict is read from `useWorkflowEditStore.draft` (the
 * source of truth for in-flight edits) and shipped in the request body
 * (M2) so debug-execute targets the UNSAVED node, not the committed
 * snapshot.
 *
 * readOnly (a pinned/historical version): debug-execute is STILL allowed —
 * the run is ephemeral and persists nothing, so running a node to inspect
 * its behaviour is safe even on a pinned version. Only graph *edits* are
 * blocked elsewhere; this read-only debug action is intentionally enabled.
 *
 * The SSE runner is injected (`runner` prop, defaulting to
 * `streamNodeExecution`) so the panel is unit-testable without mocking the
 * network.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNodes } from '@xyflow/react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { FieldValueWidget } from '@/pages/canvas/inspector/FieldValueWidget';
import { useNodeExecStore, nodeInputsKey } from '@/stores/node-exec';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useRunNodeResult } from '@/lib/api/queries/vfs';
import {
  streamNodeExecution,
  type StreamNodeExecutionArgs,
} from '@/lib/api/sse/node-exec-stream';
import { parseRenderedResult } from '@/pages/canvas/nodes/template-preview-media';
import { RenderedPreview } from '@/pages/canvas/nodes/RenderedPreview';
import { formatBytes } from '@/lib/format/bytes';
import { cancelExecution } from '@/lib/api/executions';

/** Optional rendered-preview descriptor for a TemplateNode result box. */
interface Renderable {
  rendered: string;
  format: string;
  wfId?: string;
  runId?: string;
}

interface FieldSlot {
  type?: string;
  value?: unknown;
  reference?: string;
}
type FieldsMap = Record<string, FieldSlot>;

export interface NodeExecutePanelProps {
  wfId: string;
  /** Injected for tests; defaults to the real SSE client. */
  runner?: (args: StreamNodeExecutionArgs) => Promise<void>;
  /** Injected for tests; defaults to the real server-side cancel request. */
  canceller?: (execId: string) => Promise<void>;
}

export function NodeExecutePanel({
  wfId,
  runner = streamNodeExecution,
  canceller = cancelExecution,
}: NodeExecutePanelProps) {
  const { t } = useTranslation();
  const nodes = useNodes();
  const selected = nodes.find((n) => n.selected);

  if (!selected) {
    return (
      <p className="text-sm text-muted-foreground">
        {t('inspector.runNode.selectNode', 'Select a node to run it.')}
      </p>
    );
  }

  return (
    <NodeExecuteForm
      key={selected.id}
      wfId={wfId}
      nodeId={selected.id}
      runner={runner}
      canceller={canceller}
    />
  );
}

interface NodeExecuteFormProps {
  wfId: string;
  nodeId: string;
  runner: (args: StreamNodeExecutionArgs) => Promise<void>;
  canceller: (execId: string) => Promise<void>;
}

function NodeExecuteForm({ wfId, nodeId, runner, canceller }: NodeExecuteFormProps) {
  const { t } = useTranslation();
  const draft = useWorkflowEditStore((s) => s.draft);
  const status = useNodeExecStore((s) => s.status);
  const result = useNodeExecStore((s) => s.result);
  const error = useNodeExecStore((s) => s.error);
  const audit = useNodeExecStore((s) => s.resource_audit);
  const storeNodeId = useNodeExecStore((s) => s.nodeId);
  const storeWfId = useNodeExecStore((s) => s.wfId);

  // The live draft node (M2): the source of truth for in-flight edits.
  const node = (draft?.[nodeId] ?? {}) as Record<string, unknown>;
  const nodeType = (node.node_type as string) ?? 'UnknownNode';
  // Title shows the user-facing node_name (what they reference other nodes by),
  // falling back to the type when a node hasn't been named yet.
  const nodeName =
    typeof node.node_name === 'string' && node.node_name
      ? (node.node_name as string)
      : nodeType;
  const inputFields = useMemo(
    () => (node.input_fields as FieldsMap) ?? {},
    [node.input_fields],
  );
  const fieldNames = useMemo(() => Object.keys(inputFields), [inputFields]);

  // Per-node data from the last whole-workflow run, gated on workflow id so
  // a run on another workflow never bleeds in. After a workflow run, this holds
  // the node's RESOLVED inputs + its output/error — the data the user wants to
  // inspect + re-run from. `result` is a JSON string; `inputs` is the resolved
  // values object the engine actually fed the node.
  const execWfId = useExecStreamStore((s) => s.wfId);
  const runNode = useExecStreamStore((s) => s.perNode[nodeId]);
  const liveRunData = execWfId === wfId ? runNode : undefined;

  // PERSISTED source: workflow /run is stable and keyed by wfId. When the live
  // exec-stream store has no resolved inputs for this node, read
  // `/run/__exec__/nodes/{nodeId}.json` from that workflow run tier. This is a
  // LOCAL query — it never writes into the global `useExecStreamStore.perNode`,
  // so the canvas nodes are NOT re-colored.
  const liveHasInputs = !!liveRunData && liveRunData.inputs !== undefined;
  const fileResult = useRunNodeResult(
    liveHasInputs ? null : wfId,
    nodeId,
  );
  // Map the run-file shape onto the live store's shape: `output` (a value, e.g.
  // a TemplateNode `{rendered,format}` dict) becomes the `result` JSON STRING
  // ExecResultBox/renderableFor expect, while inputs/status/error pass through.
  // Memoized on `fileResult` (stable now that useRunNodeResult is staleTime:
  // Infinity) so the potentially-large `JSON.stringify(output)` runs ONCE per
  // result and `runData` stays a stable reference (no per-render effect churn).
  const fileRunData = useMemo(
    () =>
      fileResult &&
      (fileResult.output !== undefined ||
        fileResult.error !== undefined ||
        fileResult.inputs !== undefined)
        ? {
            status: fileResult.status,
            resource_audit: fileResult.resource_audit,
            inputs: fileResult.inputs,
            result:
              fileResult.output !== undefined
                ? JSON.stringify(fileResult.output)
                : undefined,
            error: fileResult.error,
          }
        : undefined,
    [fileResult],
  );
  // Prefer the live data when it carries inputs; otherwise fall back to the
  // persisted run-file (and if neither, keep whatever the live store had).
  const runData = liveHasInputs ? liveRunData : (fileRunData ?? liveRunData);

  // TemplateNode rendered-preview wiring: a "Render" toggle on the output box
  // previews the `rendered` field per the node's declared output_format. Only
  // applies to TemplateNode results that parse to `{ rendered: "…" }`.
  const isTemplate = (node.node_type as string) === 'TemplateNode';
  const outputFormat =
    typeof (node.node_config as Record<string, unknown>)?.output_format ===
    'string'
      ? (node.node_config as Record<string, string>).output_format
      : 'text';
  const renderableFor = (
    resultStr?: string,
    runId?: string,
  ): Renderable | undefined => {
    if (!isTemplate) return undefined;
    const parsed = parseRenderedResult(resultStr);
    if (!parsed) return undefined;
    // Prefer the format carried IN the output; fall back to the node's
    // configured output_format, then 'text'.
    const format = parsed.format || outputFormat || 'text';
    return { rendered: parsed.rendered, format, wfId, runId };
  };

  // Per-field input buffer, seeded (highest precedence first):
  //   1. the last workflow run's RESOLVED inputs for this node (so right after
  //      a run, the panel reflects what actually ran — the debug starting point);
  //   2. else the user's last manual debug inputs (persisted across remounts);
  //   3. else the node's configured field values.
  // Local panel state (NOT the edit store) — debug inputs are ephemeral and
  // must not mutate the workflow draft.
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const seed: Record<string, unknown> = {};
    for (const [name, slot] of Object.entries(inputFields)) {
      seed[name] = slot.value;
    }
    const persisted =
      useNodeExecStore.getState().inputsByNode[nodeInputsKey(wfId, nodeId)];
    let base = persisted ? { ...seed, ...persisted } : seed;
    // Overlay the last workflow run's resolved inputs (read the store snapshot
    // directly so this is computed once at mount; clicking a node AFTER a run
    // remounts this form on its `key={nodeId}`, re-seeding from the run).
    const ex = useExecStreamStore.getState();
    const runInputs =
      ex.wfId === wfId ? ex.perNode[nodeId]?.inputs : undefined;
    if (runInputs && typeof runInputs === 'object' && !Array.isArray(runInputs)) {
      const ri = runInputs as Record<string, unknown>;
      for (const name of Object.keys(inputFields)) {
        if (name in ri) base = { ...base, [name]: ri[name] };
      }
    }
    return base;
  });
  // Whether the form has already been seeded from a run's resolved inputs — set
  // at mount if the LIVE store carried them, or once the user edits a field, or
  // once the async PERSISTED inputs arrive. Once set, the persisted-prefill
  // effect below is a no-op, so it can NEVER clobber user typing or a later
  // live run (user input always wins).
  const seeded = useRef(liveHasInputs);
  // Persist the input buffer per node on every change so a sider re-entry (the
  // form remounts on its `key={nodeId}`) re-hydrates from the store above —
  // fixing "inputs vanish after leaving + re-entering, only the result remained".
  useEffect(() => {
    useNodeExecStore.getState().setNodeInputs(wfId, nodeId, values);
  }, [wfId, nodeId, values]);
  // PERSISTED-inputs prefill (Fix 2): the live store has no inputs at mount but
  // the persisted execution's inputs arrive async (after mount). When they do —
  // and only if the form hasn't already been seeded from a run and the user
  // hasn't edited — re-seed just the KNOWN input field names. Idempotent
  // (guarded by `seeded`), so it doesn't fight the `setNodeInputs` persistence.
  useEffect(() => {
    if (seeded.current) return;
    const ri = runData?.inputs;
    if (!ri || typeof ri !== 'object' || Array.isArray(ri)) return;
    const src = ri as Record<string, unknown>;
    seeded.current = true;
    queueMicrotask(() => setValues((prev) => {
      const next = { ...prev };
      for (const name of fieldNames) {
        if (name in src) next[name] = src[name];
      }
      return next;
    }));
  }, [fieldNames, runData]);
  const execIdRef = useRef<string | null>(null);
  const stopRequestedRef = useRef(false);
  const cancelPromiseRef = useRef<Promise<void> | null>(null);
  const [stopping, setStopping] = useState(false);
  // Only show this panel's run output when the store's run is for THIS node IN
  // THIS workflow (node ids collide across workflows — gate on both so a run
  // started on another workflow's same-numbered node never paints here).
  const isThisNode = storeWfId === wfId && storeNodeId === nodeId;
  const running = isThisNode && status === 'running';

  const requestServerStop = (execId: string): Promise<void> => {
    if (cancelPromiseRef.current) return cancelPromiseRef.current;
    const pending = canceller(execId).finally(() => {
      if (cancelPromiseRef.current === pending) cancelPromiseRef.current = null;
    });
    cancelPromiseRef.current = pending;
    return pending;
  };

  const onRun = async () => {
    const input: Record<string, unknown> = {};
    for (const name of fieldNames) input[name] = values[name] ?? '';

    const ac = new AbortController();
    execIdRef.current = null;
    stopRequestedRef.current = false;
    setStopping(false);
    try {
      await runner({
        wfId,
        nodeId,
        node,
        input,
        ac,
        onExecutionStarted: (execId) => {
          execIdRef.current = execId;
          if (stopRequestedRef.current) {
            void requestServerStop(execId).catch(() => setStopping(false));
          }
        },
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        useNodeExecStore.getState().setStatus('cancelled');
      }
      // Other errors already flipped status to 'error' in the runner.
    } finally {
      setStopping(false);
    }
  };

  const onStop = async () => {
    stopRequestedRef.current = true;
    setStopping(true);
    const execId = execIdRef.current;
    if (!execId) return;
    try {
      await requestServerStop(execId);
      // Keep SSE connected: the backend publishes `cancelled` only after the
      // sandbox job is actually terminated and its terminal frame persisted.
    } catch {
      stopRequestedRef.current = false;
      setStopping(false);
    }
  };

  return (
    <div className="space-y-4 select-text" data-testid="node-execute-panel">
      <div className="space-y-1">
        <div className="text-sm font-medium" data-testid="node-exec-title">
          {/* Dynamic node_name rendered as JSX (NOT i18n interpolation) so the
              user-facing name always shows verbatim. Verb reuses the Run key. */}
          {t('inspector.runNode.run', 'Run')} {nodeName}{' '}
          <span className="text-muted-foreground">({nodeId})</span>
        </div>
        <p className="text-xs text-muted-foreground">
          {t(
            'inspector.runNode.hint',
            'Give this node inputs and run it in isolation to inspect its output. References are ignored — supply values directly.',
          )}
        </p>
      </div>

      {fieldNames.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {t('inspector.runNode.noInputs', 'This node has no input fields.')}
        </p>
      ) : (
        <div className="space-y-3">
          {fieldNames.map((name) => {
            const fieldType = inputFields[name]?.type ?? 'string';
            return (
              <div key={name} className="space-y-1">
                <Label className="text-xs">
                  {name}{' '}
                  <span className="text-muted-foreground">({fieldType})</span>
                </Label>
                <FieldValueWidget
                  type={fieldType}
                  value={values[name]}
                  allowReference={false}
                  deferCoercion
                  idBase={`node-exec-${name}`}
                  onChange={(next) => {
                    // User edit wins: stop any later persisted-prefill seeding.
                    seeded.current = true;
                    setValues((prev) => ({ ...prev, [name]: next.value }));
                  }}
                />
              </div>
            );
          })}
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          data-testid="node-exec-run"
          disabled={running}
          onClick={onRun}
        >
          {running
            ? t('inspector.runNode.running', 'Running…')
            : t('inspector.runNode.run', 'Run')}
        </Button>
        {running && (
          <Button
            size="sm"
            variant="outline"
            data-testid="node-exec-stop"
            disabled={stopping}
            onClick={onStop}
          >
            {stopping
              ? t('inspector.runNode.stopping', 'Stopping…')
              : t('inspector.runNode.stop', 'Stop')}
          </Button>
        )}
        {running && (
          <span
            className="h-4 w-4 animate-spin rounded-full border-2 border-muted border-t-foreground"
            data-testid="node-exec-spinner"
            aria-label="running"
          />
        )}
      </div>

      {/* The node's result/error from the LAST whole-workflow run — so after
          one workflow run the user can click any node and immediately inspect
          what it produced (and re-run it above to debug). Shown independently
          of the isolated node-debug run below. */}
      {runData &&
        (runData.result !== undefined || runData.error !== undefined) &&
        !(isThisNode && status !== 'idle') && (
          <div className="space-y-1" data-testid="node-exec-lastrun">
            <div className="text-xs text-muted-foreground">
              {t('inspector.runNode.lastRun', 'Last workflow run')}
              {runData.status ? (
                <span className="ml-1 font-medium" data-testid="node-exec-lastrun-status">
                  · {runData.status}
                </span>
              ) : null}
            </div>
            {runData.result !== undefined && (
              <ExecResultBox
                result={runData.result}
                testId="node-exec-lastrun-result"
                toggleTestId="node-exec-lastrun-format-toggle"
                renderable={renderableFor(runData.result, wfId)}
              />
            )}
            <ResourceAuditPanel audit={runData.resource_audit} />
            {runData.error !== undefined && (
              <pre
                className="text-xs bg-destructive/10 text-destructive p-2 rounded max-h-64 overflow-auto whitespace-pre-wrap break-words select-text"
                data-testid="node-exec-lastrun-error"
              >
                {runData.error}
              </pre>
            )}
          </div>
        )}

      {isThisNode && (status !== 'idle') && (
        <div className="space-y-1" data-testid="node-exec-log">
          <div className="text-xs">
            {t('inspector.runNode.status', 'Status:')}{' '}
            <span className="font-medium" data-testid="node-exec-status">
              {status}
            </span>
          </div>
          {result !== undefined && (
            <ExecResultBox
              result={result}
              renderable={renderableFor(result, undefined)}
            />
          )}
          <ResourceAuditPanel audit={audit} />
          {error !== undefined && (
            <pre
              className="text-xs bg-destructive/10 text-destructive p-2 rounded max-h-64 overflow-auto whitespace-pre-wrap break-words select-text"
              data-testid="node-exec-error"
            >
              {error}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Pretty-print a JSON-string result with 2-space indentation, but only for
 * STRUCTURED values (objects / arrays) — a bare string/number isn't worth a
 * "Format" toggle. Returns null when the result isn't parseable JSON or isn't
 * structured, so the caller hides the button.
 */
function tryPrettyJson(raw: string): string | null {
  try {
    const parsed = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object') return null;
    return JSON.stringify(parsed, null, 2);
  } catch {
    return null;
  }
}

/** Cheap "is this structured JSON?" check — first non-space char is {/[ — used
 *  to decide whether to OFFER the Format toggle WITHOUT parsing the whole (maybe
 *  multi-MB) string on every render. */
function looksStructured(raw: string): boolean {
  const c = raw.trimStart()[0];
  return c === '{' || c === '[';
}

// Cap the characters rendered into the <pre>: a multi-MB result laid out with
// whitespace-pre-wrap janks the panel. Show the first slice + a "Show all".
const MAX_INLINE_CHARS = 100_000;

/**
 * The run-output box: a FIXED-height, vertically-scrollable pane (so long
 * outputs don't push the panel) with a top-right "Format ⇆ Restore" toggle.
 * Format shows the result indented (indent=2) for readability; Restore returns
 * the raw single-line/string result. The toggle only renders when the result
 * is structured JSON (otherwise there's nothing to format). The toggle resets
 * to "raw" whenever a new run replaces the result.
 */
function ExecResultBox({
  result,
  testId = 'node-exec-result',
  toggleTestId = 'node-exec-format-toggle',
  renderable,
}: {
  result: string;
  testId?: string;
  toggleTestId?: string;
  renderable?: Renderable;
}) {
  const { t } = useTranslation();
  const [formatted, setFormatted] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const separated = useMemo(() => {
    if (!result.includes('"__traces__"')) return { output: result, traces: null };
    try {
      const value = JSON.parse(result);
      if (!value || Array.isArray(value) || !Array.isArray(value.__traces__)) return { output: result, traces: null };
      const { __traces__, ...business } = value;
      return { output: JSON.stringify(business), traces: __traces__ as unknown[] };
    } catch { return { output: result, traces: null }; }
  }, [result]);
  const outputText = separated.output;
  // Cheap check (no parse); the Format toggle only PARSES on demand below.
  const canFormat = useMemo(() => looksStructured(outputText), [outputText]);
  useEffect(() => {
    queueMicrotask(() => {
      setFormatted(false);
      setRendering(false);
      setExpanded(false);
    });
  }, [result]);

  // Parse+pretty-print ONLY when the user actually toggled Format — not eagerly
  // on every render/tab-switch (that was the big-result CPU hit).
  const shown = useMemo(() => {
    if (formatted && canFormat) {
      const p = tryPrettyJson(outputText);
      if (p != null) return p;
    }
    return outputText;
  }, [formatted, canFormat, outputText]);

  const truncated = !expanded && shown.length > MAX_INLINE_CHARS;
  const display = truncated ? shown.slice(0, MAX_INLINE_CHARS) : shown;

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">
          {t('inspector.runNode.output', 'Output')}
        </span>
        <div className="flex items-center gap-1">
          {canFormat && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs text-muted-foreground"
              data-testid={toggleTestId}
              onClick={() => setFormatted((v) => !v)}
            >
              {formatted
                ? t('inspector.runNode.restore', 'Restore')
                : t('inspector.runNode.format', 'Format')}
            </Button>
          )}
          {renderable && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs text-muted-foreground"
              data-testid={`${toggleTestId}-render`}
              onClick={() => setRendering((v) => !v)}
            >
              {rendering
                ? t('inspector.runNode.hideRender', 'Hide')
                : t('inspector.runNode.render', 'Render')}
            </Button>
          )}
        </div>
      </div>
      <pre
        className="text-xs bg-muted p-2 rounded h-64 overflow-auto whitespace-pre-wrap break-words select-text"
        data-testid={testId}
      >
        {display}
      </pre>
      {truncated && (
        <button
          type="button"
          className="text-xs text-muted-foreground underline-offset-2 hover:underline"
          data-testid={`${testId}-show-all`}
          onClick={() => setExpanded(true)}
        >
          {t('inspector.runNode.showAll', 'Output truncated — show all ({{size}})', {
            size: formatBytes(shown.length),
          })}
        </button>
      )}
      {separated.traces && <ExecutionTraces messages={separated.traces} />}
      {renderable && rendering && <RenderedPreview {...renderable} />}
    </div>
  );
}
