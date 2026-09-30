import { parseResourceAudit, type ResourceAudit } from '@/lib/resource-audit';
/**
 * State store for single-node debug execution.
 *
 * Deliberately separate from `exec-stream.ts`, which owns
 * whole-WORKFLOW run + canvas highlighting). Node-debug is an isolated,
 * ephemeral surface: you click ONE node, give it inputs, Run, watch a
 * spinner, read its output log, and can Stop. It must NOT clobber the
 * workflow exec-stream store (a node-debug run mid-edit shouldn't paint
 * the canvas as "running") nor be clobbered by it.
 *
 * It models a SINGLE node run at a time (matching the non-technical
 * single-run mental model — M4): `begin()` wipes the previous run's log.
 * The panel disables Run while `status === 'running'`.
 *
 * Frame shape consumed (SSE wire, not in `schema.d.ts`):
 *   { node_id?, status, result?, error?, exec_id? }
 * which is the SAME shape `run_node_to_frames` synthesizes on the backend
 * (`services/node_exec.py`), so the rendering primitives stay uniform with
 * the workflow Execution tab.
 *
 * `subscribeWithSelector` so the SSE client module can read/write state
 * outside React via `getState()` (mirrors `exec-stream.ts`).
 */
import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null;
}

export type NodeExecStatus =
  | 'idle'
  | 'running'
  | 'completed'
  | 'error'
  | 'cancelled';

/**
 * Composite key for the per-node input buffer. Node ids (`node_1`, …) are NOT
 * unique across workflows, so keying by node id alone would bleed one
 * workflow's debug inputs into another's same-numbered node. Scope by wfId.
 */
export function nodeInputsKey(wfId: string, nodeId: string): string {
  // Keep the collision-proof separator explicit in source. A literal NUL made
  // Git and text-review tools classify this TypeScript file as binary.
  return `${wfId}\u0000${nodeId}`;
}

export interface NodeExecState {
  resource_audit?: ResourceAudit;
  /** The workflow the current run belongs to (node ids collide across
   * workflows, so gate the rendered output on BOTH wfId and nodeId). */
  wfId: string | null;
  /** The node currently bound to the panel run (so a stale run from a
   * different node never paints the wrong output). */
  nodeId: string | null;
  status: NodeExecStatus;
  /** JSON-string result of the terminal `completed` frame. */
  result?: string;
  /** Error text of a terminal `error` frame. */
  error?: string;
  abortController: AbortController | null;
  /**
   * The debug-input values the user last entered, KEYED BY {@link nodeInputsKey}
   * (wfId + node_id), so they survive leaving + re-entering the inspector (the
   * panel is local state that unmounts; this store is a module singleton that
   * does not). The result was already persisted this way; the inputs were not —
   * so a re-entry showed the result but lost the inputs that produced it. Keyed
   * per (workflow, node) so neither switching nodes NOR switching workflows
   * bleeds one node's inputs into another's.
   */
  inputsByNode: Record<string, Record<string, unknown>>;

  begin: (nodeId: string, ac: AbortController, wfId: string) => void;
  applyUpdate: (update: unknown) => void;
  setStatus: (s: NodeExecStatus) => void;
  /** Persist (replace) the input buffer for one (workflow, node). */
  setNodeInputs: (
    wfId: string,
    nodeId: string,
    values: Record<string, unknown>,
  ) => void;
  reset: () => void;
}

export const useNodeExecStore = create<NodeExecState>()(
  subscribeWithSelector((set) => ({
    wfId: null,
    nodeId: null,
    status: 'idle',
    result: undefined,
    resource_audit: undefined,
    error: undefined,
    abortController: null,
    inputsByNode: {},

    begin: (nodeId, abortController, wfId) =>
      set({
        wfId,
        nodeId,
        abortController,
        status: 'running',
        result: undefined,
        resource_audit: undefined,
        error: undefined,
      }),

    applyUpdate: (update) =>
      set((s) => {
        if (!isObject(update)) return s;
        const status =
          typeof update.status === 'string' ? update.status : undefined;
        const result =
          typeof update.result === 'string' ? update.result : undefined;
        const error =
          typeof update.error === 'string' ? update.error : undefined;

        // Map the synthesized frame status onto the panel's status enum.
        // 'running' keeps the spinner; 'completed'/'error' are terminal.
        const next: Partial<NodeExecState> = {};
        if ('resource_audit' in update) next.resource_audit = parseResourceAudit(update.resource_audit);
        if (status === 'completed') {
          next.status = 'completed';
          if (result !== undefined) next.result = result;
        } else if (status === 'error') {
          next.status = 'error';
          if (result !== undefined) next.result = result;
          if (error !== undefined) next.error = error;
        } else if (status === 'cancelled') {
          next.status = 'cancelled';
        } else if (status === 'running') {
          next.status = 'running';
        }
        return { ...s, ...next };
      }),

    setStatus: (status) => set({ status }),

    setNodeInputs: (wfId, nodeId, values) =>
      set((s) => ({
        inputsByNode: {
          ...s.inputsByNode,
          [nodeInputsKey(wfId, nodeId)]: values,
        },
      })),

    reset: () =>
      set({
        wfId: null,
        nodeId: null,
        status: 'idle',
        result: undefined,
        resource_audit: undefined,
        error: undefined,
        abortController: null,
      }),
  })),
);
