import { parseResourceAudit, type ResourceAudit } from '@/lib/resource-audit';
/**
 * Execution-stream store — holds the in-flight workflow execution.
 *
 * One execution is the round-trip from clicking Execute to the backend
 * emitting `event: done` on `/api/v1/workflows/{wf_id}/executions`. While
 * the run is alive we accumulate every `EXEC_UPDATE` payload into a flat
 * `Record<node_id, {status, result?, error?}>` map keyed by node, so the
 * Execution tab can render per-node status badges without a second fetch
 * to the canonical execution-status endpoint.
 *
 * Why a separate store from `chat-stream`:
 *   - The two SSE channels are independent (different POST endpoints,
 *     different lifecycle). Trying to share the buffer would force the
 *     route-signal layer to discriminate, and the UI consumers don't
 *     overlap (chat sidebar vs. inspector Execution tab).
 *   - Cancellation paths differ: chat just `.abort()`s the fetch; exec
 *     additionally hits a REST cancel endpoint so the backend can clean
 *     up the engine task even if the SSE socket is already torn down.
 *
 * Mirrors `chat-stream.ts` patterns:
 *   - `subscribeWithSelector` so the streamer module can read state
 *     outside React via `getState()`.
 *   - `abortController` is stored so the Cancel button can fire `.abort()`
 *     without prop-drilling.
 *   - `reset()` flips state back to `idle` for a clean slate; called when
 *     navigating away from the canvas page (future hook in T17).
 *
 * EXEC_UPDATE payload shape (not in OpenAPI — it's SSE wire format):
 *   { node_id?, status, inputs?, result?, error? }
 * We narrow defensively in `applyUpdate` because the wire frames aren't
 * typed by `schema.d.ts`.
 */
import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';

/** Type guard: payload is a non-null object we can probe with `in`. */
function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null;
}

function execStoreDebug(message: string, data?: unknown): void {
  const enabled =
    import.meta.env.VITE_EXEC_DEBUG === '1' ||
    (typeof window !== 'undefined' &&
      window.localStorage.getItem('vc_exec_debug') === '1');
  if (!enabled) return;

  console.info(`[workflow-exec-store] ${message}`, data ?? '');
}

function closeRunningNodes(
  perNode: Record<string, ExecNodeState>,
  status: 'completed' | 'cancelled' | 'error',
): Record<string, ExecNodeState> {
  let changed = false;
  const next: Record<string, ExecNodeState> = {};
  for (const [nodeId, node] of Object.entries(perNode)) {
    if (node.status === 'running') {
      changed = true;
      next[nodeId] = {
        ...node,
        status,
        error:
          status === 'error'
            ? (node.error ?? 'Execution stopped before this node finished.')
            : node.error,
      };
    } else {
      next[nodeId] = node;
    }
  }
  return changed ? next : perNode;
}

export type ExecStatus =
  | 'idle'
  | 'running'
  | 'completed'
  | 'error'
  | 'cancelled';

/**
 * Per-node row materialised from EXEC_UPDATE frames. `inputs` is left
 * `unknown` because the backend will emit arbitrary JSON-typed values
 * once full engine streaming lands (T17); for now most frames only
 * populate `status` and the final frame's `result` / `error`.
 */
export interface ExecNodeState {
  resource_audit?: ResourceAudit;
  status: string;
  inputs?: unknown;
  result?: string;
  error?: string;
  /** Per-node wall-clock seconds (the backend folds the engine's
   * `execution_time` onto the per-node frame). Shown next to the node's
   * "completed" status in the Run output. Undefined until the node finishes. */
  duration?: number;
}

export interface ExecStreamState {
  /**
   * The workflow this live run belongs to. The live pair
   * (`status`/`perNode`) is a module singleton, so without an owner
   * tag a run started on workflow A would still render on workflow B's Run tab
   * ("execution history" must NOT bleed across workflows). Consumers gate their
   * live reads on `wfId === <their wfId>`; a mismatch falls back to the
   * server-persisted, wfId-keyed hydration path. `null` when idle/never-run.
   */
  wfId: string | null;
  status: ExecStatus;
  perNode: Record<string, ExecNodeState>;
  /** End-to-end workflow wall-clock seconds, captured from the terminal frame's
   * `duration` (the engine's total `execution_time`). `null` until a run
   * finishes; shown on the "Status:" line. */
  totalDuration: number | null;
  abortController: AbortController | null;
  begin: (wfId: string, ac: AbortController) => void;
  applyUpdate: (update: unknown) => void;
  setStatus: (s: ExecStatus) => void;
  /**
   * The workflow-run input values the user last entered, KEYED BY wfId, so they
   * survive leaving + re-entering the inspector (the Run tab holds them in local
   * state that unmounts; this module-singleton store does not). The RESULT was
   * already persisted (perNode); the inputs were not — so a re-entry showed the
   * result but lost the inputs that produced it. Per wfId so different workflows
   * never share an input buffer. Mirrors `useNodeExecStore.inputsByNode`.
   */
  inputsByWorkflow: Record<string, Record<string, unknown>>;
  setWorkflowInputs: (wfId: string, values: Record<string, unknown>) => void;
  reset: () => void;
}

export const useExecStreamStore = create<ExecStreamState>()(
  subscribeWithSelector((set) => ({
    wfId: null,
    status: 'idle',
    perNode: {},
    totalDuration: null,
    abortController: null,
    inputsByWorkflow: {},

    setWorkflowInputs: (wfId, values) =>
      set((s) => ({
        inputsByWorkflow: { ...s.inputsByWorkflow, [wfId]: values },
      })),

    begin: (wfId, abortController) =>
      set({
        wfId,
        abortController,
        status: 'running',
        perNode: {},
        totalDuration: null,
      }),

    applyUpdate: (update) =>
      set((s) => {
        if (!isObject(update)) return s;
        const updateWfId =
          typeof update.wf_id === 'string' ? update.wf_id : undefined;
        if (updateWfId && s.wfId && updateWfId !== s.wfId) {
          execStoreDebug('ignored frame for different wf_id', {
            currentWfId: s.wfId,
            updateWfId,
            update,
          });
          return s;
        }

        const node_id =
          typeof update.node_id === 'string' ? update.node_id : null;
        if (node_id) {
          const status =
            typeof update.status === 'string' ? update.status : 'unknown';
          const result =
            typeof update.result === 'string' ? update.result : undefined;
          const error =
            typeof update.error === 'string' ? update.error : undefined;
          const duration =
            typeof update.duration === 'number' ? update.duration : undefined;
          const prev = s.perNode[node_id];
          execStoreDebug('apply node frame', {
            wfId: s.wfId,
            updateWfId,
            node_id,
            prevStatus: prev?.status,
            nextStatus: status,
          });
          return {
            perNode: {
              ...s.perNode,
              [node_id]: {
                ...prev,
                status,
                result: result ?? prev?.result,
                error: error ?? prev?.error,
                duration: duration ?? prev?.duration,
                resource_audit: parseResourceAudit(update.resource_audit) ?? (status === 'running' ? undefined : prev?.resource_audit),
                inputs: 'inputs' in update ? update.inputs : prev?.inputs,
              },
            },
          };
        }

        const next: Record<string, ExecNodeState> = { ...s.perNode };
        const totalDuration =
          typeof update.duration === 'number'
            ? update.duration
            : s.totalDuration;
        const terminalStatus: ExecStatus | null =
          update.status === 'completed'
            ? 'completed'
            : update.status === 'cancelled' || update.status === 'stopped'
              ? 'cancelled'
              : update.status === 'error' || update.status === 'failed'
              ? 'error'
              : null;
        if (terminalStatus) {
          execStoreDebug('apply terminal frame', {
            wfId: s.wfId,
            updateWfId,
            terminalStatus,
            runningNodes: Object.entries(s.perNode)
              .filter(([, node]) => node.status === 'running')
              .map(([nodeId]) => nodeId),
          });
        }

        const errors = isObject(update.errors) ? update.errors : null;
        if (errors) {
          for (const [nid, raw] of Object.entries(errors)) {
            const e = isObject(raw) ? raw : {};
            const msg =
              typeof e.error_message === 'string'
                ? e.error_message
                : typeof e.error === 'string'
                  ? e.error
                  : 'error';
            next[nid] = {
              ...next[nid],
              status: typeof e.status === 'string' ? e.status : 'error',
              error: msg,
            };
          }
        }

        const outputs = isObject(update.outputs) ? update.outputs : null;
        if (outputs && Object.keys(outputs).length > 0) {
          next['__end__'] = {
            ...next['__end__'],
            status: 'completed',
            result: JSON.stringify(outputs),
          };
        }

        const closedNext =
          terminalStatus === 'completed'
            ? closeRunningNodes(next, 'completed')
            : terminalStatus === 'cancelled'
            ? closeRunningNodes(next, 'cancelled')
            : terminalStatus === 'error'
              ? closeRunningNodes(next, 'error')
              : next;

        return {
          perNode: closedNext,
          totalDuration,
          ...(terminalStatus
            ? { status: terminalStatus, abortController: null }
            : {}),
        };
      }),

    setStatus: (status) =>
      set((s) => ({
        status,
        ...(status === 'cancelled'
          ? {
              perNode: closeRunningNodes(s.perNode, 'cancelled'),
              abortController: null,
            }
            : status === 'error'
            ? {
                perNode: closeRunningNodes(s.perNode, 'error'),
                abortController: null,
              }
            : status === 'completed'
              ? {
                  perNode: closeRunningNodes(s.perNode, 'completed'),
                  abortController: null,
                }
              : {}),
      })),

    reset: () =>
      set({
        wfId: null,
        status: 'idle',
        perNode: {},
        totalDuration: null,
        abortController: null,
      }),
  })),
);
