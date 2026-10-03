/**
 * Client-side workflow draft + undo/redo store.
 *
 * The server (vibecanvas-api) is the source of truth for committed
 * workflow state; this store holds the *in-flight* draft the user is
 * editing on the canvas, including a bounded undo/redo history. The
 * draft seeds from `useWorkflow` and is replayed onto xyflow.
 *
 * Save-state model (Stream 0a)
 * ----------------------------
 * `baseline` is the JSON of the last saved/loaded committed version.
 * **`dirty` is DERIVED**: `isDirty()` === `JSON.stringify(draft) !== baseline`.
 * We also keep a `dirty` field synced inside `set` so existing selector
 * consumers (`CanvasToolbar`) stay cheap, but the comparison is the truth.
 *   - `setDraft(wf)` seeds the draft, sets `baseline = JSON.stringify(wf)`,
 *     and wipes both undo/redo stacks (switching workflows / loading a
 *     server version must not let "undo" jump into another version's
 *     history).
 *   - `markSaved()` re-baselines to the current draft (the markClean fix —
 *     wired to `useCommitWorkflow.onSuccess`) but does NOT clear the
 *     stacks: you can undo past a save (code-editor behaviour).
 *   - `undo`/`redo` are pure in-memory draft moves; they do NOT hard-set
 *     `dirty` — after the swap `dirty` re-derives, so undoing back to the
 *     baseline bytes is clean again (Save disabled).
 *
 * Mutation seam (Stream 0c)
 * -------------------------
 * `applyEdit(mutator)` is the universal mutation seam (structuredClone +
 * undo snapshot). Every batched action (`addNodes`, `removeNodes`,
 * `connectNodes`, `disconnectNodes`, `pairNodes`, `pasteNodes`) runs
 * inside exactly ONE `applyEdit` so it is a single undo step. The shared
 * helper `syncTypeConfigOnEdgeChange` is the SOLE writer of the
 * `children ↔ next_node_id` membership invariant for Condition /
 * ParallelStart sources.
 */
import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import { applyNodeTypeDefaults } from '@/lib/workflow/node-type-defaults';
import { useExecStreamStore } from '@/stores/exec-stream';

const UNDO_LIMIT = 50;

export type WorkflowDraft = Record<string, unknown>;

/** A node payload as carried in the flat workflow dict. */
type NodeRec = Record<string, unknown>;

export interface WorkflowEditState {
  draft: WorkflowDraft | null;
  /** Synced mirror of `isDirty()` for cheap selector reads; the truth is the comparison. */
  dirty: boolean;
  /** JSON of the last saved/loaded committed version. */
  baseline: string;
  /** Version actually loaded for editing; metadata refresh must not advance it. */
  baseVersion: string | null;
  undoStack: string[];
  redoStack: string[];
  /** In-memory node clipboard (Stream 0c — NOT the OS clipboard). */
  clipboard: NodeRec[];
  setDraft: (wf: WorkflowDraft | null, version?: string) => void;
  /**
   * Merge a fresh server `__meta__` (e.g. after a rename) into BOTH the draft
   * and the baseline WITHOUT touching the draft's graph — so the new name
   * takes effect, the user's unsaved graph edits survive, and `dirty` stays
   * correctly derived (the meta change is rebased into the baseline, not
   * counted as an unsaved edit). No-op while the draft is null.
   */
  applyServerMeta: (meta: unknown) => void;
  applyEdit: (mutator: (wf: WorkflowDraft) => WorkflowDraft) => void;
  addNode: (payload: Record<string, unknown>, position: { x: number; y: number }) => void;
  addNodes: (
    payloads: Record<string, unknown>[],
    positions?: { x: number; y: number }[],
  ) => void;
  removeNode: (id: string) => void;
  removeNodes: (ids: string[]) => void;
  connectNodes: (source: string, target: string) => void;
  disconnectNodes: (source: string, target: string) => void;
  pairNodes: (aId: string, bId: string, kind: 'parallel' | 'loop') => void;
  copyNodes: (ids: string[]) => void;
  pasteNodes: (anchorPos: { x: number; y: number }) => void;
  /** DERIVED save-state truth: draft diverges from the last baseline. */
  isDirty: () => boolean;
  markSaved: (version?: string, savedDraft?: WorkflowDraft) => void;
  /** @deprecated kept for back-compat; prefer `markSaved`. */
  markClean: () => void;
  undo: () => void;
  redo: () => void;
}

// ---------------------------------------------------------------------------
// Pure helpers (exported for unit tests).
// ---------------------------------------------------------------------------

function isNodeKey(key: string): boolean {
  return /^node_\d+$/.test(key);
}

function asNode(wf: WorkflowDraft, id: string): NodeRec | null {
  const v = wf[id];
  return v && typeof v === 'object' ? (v as NodeRec) : null;
}

function nodeType(node: NodeRec | null): string | undefined {
  return node?.node_type as string | undefined;
}

function getConfig(node: NodeRec): NodeRec {
  let cfg = node.node_config as NodeRec | undefined;
  if (!cfg || typeof cfg !== 'object') {
    cfg = {};
    node.node_config = cfg;
  }
  return cfg;
}

function getChildren(node: NodeRec): string[] {
  if (!Array.isArray(node.children)) node.children = [];
  return node.children as string[];
}

/**
 * Allocate the next `node_{N}` id given the keys already present, advancing
 * a local set so a batch of inserts in ONE mutator never collides.
 */
function nextNodeId(existing: Set<string>): string {
  let max = 0;
  for (const k of existing) {
    if (isNodeKey(k)) {
      const n = Number.parseInt(k.slice(5), 10);
      if (Number.isFinite(n) && n > max) max = n;
    }
  }
  return `node_${max + 1}`;
}

/**
 * Is this condition card the mandatory `others` fallback? The runtime detects
 * it by `condition_str.strip() == "others"` (condition.py), NOT by name, so
 * that is the authoritative test. We also accept `condition_name === 'others'`
 * defensively for cards seeded only by name.
 */
function isOthersCard(c: NodeRec): boolean {
  return (
    (c.condition_str as string | undefined)?.trim() === 'others' ||
    c.condition_name === 'others'
  );
}

/**
 * Guarantee the ConditionNode `conditions` invariant after any mutation:
 *   1. EXACTLY one `others` card exists — create
 *      `{condition_name:'others', condition_str:'others', next_node_id:null}`
 *      if none is present (extras beyond the first are demoted to plain rows
 *      so we never silently drop a user's target).
 *   2. The single `others` card is the LAST element of the array.
 *
 * Mutates + returns the same array. Call at the END of every ConditionNode
 * mutation.
 */
export function normalizeConditions(conditions: NodeRec[]): NodeRec[] {
  const others = conditions.filter(isOthersCard);
  const rest = conditions.filter((c) => !isOthersCard(c));
  let othersCard: NodeRec;
  if (others.length === 0) {
    othersCard = {
      condition_name: 'others',
      condition_str: 'others',
      next_node_id: null,
    };
  } else {
    othersCard = others[0];
    // Demote any extra "others" cards to plain rows (keep their target).
    for (let i = 1; i < others.length; i++) {
      const extra = others[i];
      extra.condition_str = '';
      if (extra.condition_name === 'others') extra.condition_name = '';
      rest.push(extra);
    }
  }
  // Rewrite in place: non-others first (original order), others last.
  conditions.length = 0;
  conditions.push(...rest, othersCard);
  return conditions;
}

/**
 * SOLE writer of the `children ↔ next_node_id` membership invariant for
 * type-aware sources (Condition / ParallelStart) on a connect/disconnect.
 *
 * Returns `true` on success, `false` when the op was rejected (e.g. a
 * LoopBegin would gain a 2nd child) so the caller can leave the draft
 * unchanged and surface a toast.
 *
 * Contract (match config entries by `next_node_id === target`, NEVER by
 * name — rename-safe):
 *   - ConditionNode 'add': idempotent if `target` is already mapped. Else, if
 *     the `others` card has no target yet, the FIRST connection becomes the
 *     default — `others.next_node_id = target`. Otherwise append a new card
 *     `{condition_name:"branch_<k>", condition_str:"", next_node_id:target}`
 *     (k = non-others count + 1) BEFORE `others`. The runtime detects the
 *     fallback by `condition_str.strip()=="others"` (condition.py), NOT by
 *     name. `normalizeConditions` keeps `others` last.
 *   - ConditionNode 'remove': the card whose `next_node_id === target` is
 *     DELETED if it is a real branch, or NULLed (kept) if it is `others`.
 *   - ParallelStart 'add': append `branches["branch_N"] =
 *     {branch_description:"", next_node_id:target}` (dedup by next_node_id).
 *   - ParallelStart 'remove': DROP the matching branch (match by
 *     next_node_id, never by name — Q1).
 *   - LoopBegin 'add': reject a 2nd child (children ≤ 1).
 *   - else: caller already owns `children`; nothing type-specific to do.
 *
 * NOTE: this helper does NOT touch `children` itself for Condition/Parallel
 * — the caller (connect/disconnect/removeNode) owns the children array; this
 * keeps the two writes co-located in the one mutator while leaving the
 * children edit explicit at the call site.
 */
export function syncTypeConfigOnEdgeChange(
  wf: WorkflowDraft,
  source: string,
  target: string,
  op: 'add' | 'remove',
): boolean {
  const node = asNode(wf, source);
  if (!node) return true;
  const type = nodeType(node);

  if (type === 'ConditionNode') {
    const cfg = getConfig(node);
    let conditions = cfg.conditions as NodeRec[] | undefined;
    if (!Array.isArray(conditions)) {
      conditions = [];
      cfg.conditions = conditions;
    }
    // Ensure an "others" card exists before we reason about it.
    if (!conditions.some(isOthersCard)) {
      conditions.push({
        condition_name: 'others',
        condition_str: 'others',
        next_node_id: null,
      });
    }
    if (op === 'add') {
      // Already mapped? (idempotent)
      const already = conditions.some((c) => c.next_node_id === target);
      if (!already) {
        const othersCard = conditions.find(isOthersCard)!;
        if (othersCard.next_node_id == null) {
          // FIRST connection becomes the default/fallback target.
          othersCard.next_node_id = target;
        } else {
          // Append a new branch card BEFORE others.
          const branchCount = conditions.filter((c) => !isOthersCard(c)).length;
          conditions.push({
            condition_name: `branch_${branchCount + 1}`,
            condition_str: '',
            next_node_id: target,
          });
        }
      }
    } else {
      // remove: DELETE the matching real-branch card; NULL it if it's "others".
      const idx = conditions.findIndex((c) => c.next_node_id === target);
      if (idx !== -1) {
        if (isOthersCard(conditions[idx])) {
          conditions[idx].next_node_id = null;
        } else {
          conditions.splice(idx, 1);
        }
      }
    }
    normalizeConditions(conditions);
    return true;
  }

  if (type === 'ParallelStartNode') {
    const cfg = getConfig(node);
    let branches = cfg.branches as Record<string, NodeRec> | undefined;
    if (!branches || typeof branches !== 'object') {
      branches = {};
      cfg.branches = branches;
    }
    if (op === 'add') {
      const already = Object.values(branches).some(
        (b) => b.next_node_id === target,
      );
      if (!already) {
        const name = `branch_${Object.keys(branches).length + 1}`;
        branches[name] = { branch_description: '', next_node_id: target };
      }
    } else {
      // remove: DROP the matching branch (match by next_node_id, NOT name).
      for (const [name, b] of Object.entries(branches)) {
        if (b.next_node_id === target) delete branches[name];
      }
    }
    return true;
  }

  if (type === 'LoopBeginNode' && op === 'add') {
    const children = getChildren(node);
    // The body head is single; a 2nd outgoing child is rejected.
    if (children.length >= 1 && !children.includes(target)) {
      return false;
    }
    return true;
  }

  return true;
}

/**
 * Strip every reference to `ids` from a single node (children + type-aware
 * config targets + all 4 pairing pointers). Used by removeNode(s) in one
 * pass. Leaves dangling `input_fields[*].reference` strings untouched (Check
 * reports them — safer than guessing a rewrite).
 */
function scrubReferencesToIds(node: NodeRec, ids: Set<string>): void {
  // children
  if (Array.isArray(node.children)) {
    node.children = (node.children as string[]).filter((c) => !ids.has(c));
  }
  const cfg = node.node_config as NodeRec | undefined;
  if (cfg && typeof cfg === 'object') {
    // Condition: DELETE the matching real-branch card; NULL it if it's
    // "others" (same remove semantics as syncTypeConfigOnEdgeChange).
    const conditions = cfg.conditions as NodeRec[] | undefined;
    if (Array.isArray(conditions)) {
      const kept: NodeRec[] = [];
      for (const c of conditions) {
        if (typeof c.next_node_id === 'string' && ids.has(c.next_node_id)) {
          if (isOthersCard(c)) {
            c.next_node_id = null;
            kept.push(c);
          }
          // else: drop the card entirely.
        } else {
          kept.push(c);
        }
      }
      cfg.conditions = normalizeConditions(kept);
    }
    // ParallelStart branches: DROP matching branch.
    const branches = cfg.branches as Record<string, NodeRec> | undefined;
    if (branches && typeof branches === 'object') {
      for (const [name, b] of Object.entries(branches)) {
        if (typeof b.next_node_id === 'string' && ids.has(b.next_node_id)) {
          delete branches[name];
        }
      }
    }
    // Pairing pointers.
    for (const ptr of [
      'parallel_end_node_id',
      'parallel_start_node_id',
      'loop_begin_node_id',
      'loop_end_node_id',
    ]) {
      const v = cfg[ptr];
      if (typeof v === 'string' && ids.has(v)) cfg[ptr] = null;
    }
  }
}

/**
 * Reset a pasted/duplicated node payload to a DISCONNECTED state: clear
 * children, all Condition/Parallel branch targets, and all 4 pairing
 * pointers. A pasted node carrying stale targets would violate
 * conditions==children on arrival (Q4).
 */
function resetNodeTopology(node: NodeRec): void {
  node.children = [];
  const cfg = node.node_config as NodeRec | undefined;
  if (cfg && typeof cfg === 'object') {
    const conditions = cfg.conditions as NodeRec[] | undefined;
    if (Array.isArray(conditions)) {
      for (const c of conditions) c.next_node_id = null;
    }
    const branches = cfg.branches as Record<string, NodeRec> | undefined;
    if (branches && typeof branches === 'object') {
      for (const b of Object.values(branches)) b.next_node_id = null;
    }
    for (const ptr of [
      'parallel_end_node_id',
      'parallel_start_node_id',
      'loop_begin_node_id',
      'loop_end_node_id',
    ]) {
      if (ptr in cfg) cfg[ptr] = null;
    }
  }
}

/**
 * DFS over the live `children` graph: would adding `source -> target`
 * create a cycle? True iff `source` is reachable FROM `target` already
 * (so the new edge would close a loop). A diamond / fan-in is allowed —
 * only a true cycle is detected.
 */
export function wouldCreateCycle(
  wf: WorkflowDraft,
  source: string,
  target: string,
): boolean {
  if (source === target) return true;
  const seen = new Set<string>();
  const stack = [target];
  while (stack.length) {
    const cur = stack.pop() as string;
    if (cur === source) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    const node = asNode(wf, cur);
    if (node && Array.isArray(node.children)) {
      for (const c of node.children as string[]) {
        if (!seen.has(c)) stack.push(c);
      }
    }
  }
  return false;
}

/**
 * Project a workflow draft to its STRUCTURAL shape: every node's UI-only
 * `__attributes__` (x/y position, etc.) is dropped. Two drafts that differ
 * ONLY in node positions project to the same value.
 *
 * Used by `isStructuralDiff` so a pure position-drag (which mutates only
 * `__attributes__`) is NOT treated as a structural edit. Anything else — a
 * connect/disconnect, add/remove, a config or input/output field change —
 * survives the projection and registers as structural.
 */
function stripAttributes(wf: WorkflowDraft): unknown {
  const out: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(wf)) {
    if (isNodeKey(key) && val && typeof val === 'object') {
      const { __attributes__: _ignored, ...rest } = val as Record<string, unknown>;
      void _ignored;
      out[key] = rest;
    } else {
      out[key] = val;
    }
  }
  return out;
}

/**
 * Return whether an edit changes graph structure rather than only position.
 * True when the two drafts differ once node `__attributes__` are stripped.
 * A node nudge → false (exec results stay); a connect/delete/field-edit →
 * true (exec results no longer correspond to the graph → clear them).
 */
export function isStructuralDiff(
  prev: WorkflowDraft,
  next: WorkflowDraft,
): boolean {
  return JSON.stringify(stripAttributes(prev)) !== JSON.stringify(stripAttributes(next));
}

/**
 * Project a workflow to its GRAPH shape by dropping the reserved `__meta__`
 * key (the workflow name + per-workflow settings live there). Two workflows
 * that differ ONLY in `__meta__` (e.g. a rename) project to the same value.
 *
 * Used (Bug A) to tell a real agent commit (committed GRAPH changed vs the
 * loaded baseline) apart from a benign meta-only refetch (a rename) that
 * must NOT trigger the conflict toast. Unlike `stripAttributes`, this keeps
 * node `__attributes__` (positions are part of the committed graph) — it
 * only removes `__meta__`.
 */
export function stripWorkflowMeta(wf: WorkflowDraft): unknown {
  const { __meta__: _ignored, ...rest } = wf as Record<string, unknown>;
  void _ignored;
  return rest;
}

// ---------------------------------------------------------------------------
// Store.
// ---------------------------------------------------------------------------

function workflowVersion(workflow: WorkflowDraft | null): string | null {
  const meta = workflow?.__meta__ as Record<string, unknown> | undefined;
  return Number.isInteger(meta?.workflow_version) && Number.isInteger(meta?.workflow_subversion)
    ? `v${meta!.workflow_version}.sv${meta!.workflow_subversion}` : null;
}

export const useWorkflowEditStore = create<WorkflowEditState>()(
  subscribeWithSelector((set, get) => ({
    draft: null,
    dirty: false,
    baseline: 'null',
    baseVersion: null,
    undoStack: [],
    redoStack: [],
    clipboard: [],

    setDraft: (wf, version) =>
      set({
        draft: wf,
        dirty: false,
        baseline: JSON.stringify(wf),
        baseVersion: version ?? workflowVersion(wf),
        undoStack: [],
        redoStack: [],
      }),

    applyServerMeta: (meta) =>
      set((state) => {
        if (state.draft === null) return state;
        // Re-key `__meta__` on a shallow clone (don't mutate in place — the
        // draft reference must change so xyflow / selectors re-render).
        const nextDraft: WorkflowDraft = { ...state.draft, __meta__: meta };
        // Rebase the baseline's `__meta__` too: parse the baseline, swap its
        // meta, re-serialize. Falls back gracefully if baseline isn't an
        // object (e.g. 'null'). This keeps `dirty` deriving off the GRAPH
        // delta only — the rebased meta change is NOT counted as unsaved.
        let nextBaseline = state.baseline;
        try {
          const parsed = JSON.parse(state.baseline);
          if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            (parsed as Record<string, unknown>).__meta__ = meta;
            nextBaseline = JSON.stringify(parsed);
          }
        } catch {
          // leave baseline untouched on a non-JSON baseline
        }
        return {
          draft: nextDraft,
          baseline: nextBaseline,
          dirty: JSON.stringify(nextDraft) !== nextBaseline,
        };
      }),

    applyEdit: (mutator) => {
      const prevDraft = get().draft;
      set((state) => {
        if (state.draft === null) return state;
        const prevSnapshot = JSON.stringify(state.draft);
        const cloned = structuredClone(state.draft) as WorkflowDraft;
        const next = mutator(cloned);
        const undoStack = [...state.undoStack, prevSnapshot];
        if (undoStack.length > UNDO_LIMIT) {
          undoStack.splice(0, undoStack.length - UNDO_LIMIT);
        }
        return {
          draft: next,
          dirty: JSON.stringify(next) !== state.baseline,
          undoStack,
          redoStack: [],
        };
      });
      // Clear stale execution results when graph structure changes.
      // (connect/disconnect/add/remove/config + input/output field edits — all
      // routed through this one seam). A pure position-drag (`__attributes__`
      // only) is excluded by `isStructuralDiff`, so a node nudge keeps the
      // breathing rings + Run-tab results. Skipped while idle so we don't churn
      // the exec store on every edit; the reset is cheap but selector-stable.
      const nextDraft = get().draft;
      if (prevDraft && nextDraft && isStructuralDiff(prevDraft, nextDraft)) {
        const exec = useExecStreamStore.getState();
        if (exec.status !== 'idle' || Object.keys(exec.perNode).length > 0) {
          exec.reset();
        }
      }
    },

    addNode: (payload, position) =>
      get().applyEdit((wf) => {
        const id = nextNodeId(new Set(Object.keys(wf)));
        const node = applyNodeTypeDefaults(structuredClone(payload) as NodeRec);
        node.node_id = id;
        // Reserved-name types (Start/End) already stamped above; everything
        // else falls back to the node id.
        if (!node.node_name) node.node_name = id;
        const attrs = (node.__attributes__ as NodeRec | undefined) ?? {};
        // Honor the requested point exactly for the first insert. When a
        // repeated palette/dialog insert targets an occupied point, use a
        // deterministic cascade instead of random jitter. This keeps replay,
        // undo/redo, tests, and collaborative state reproducible while still
        // preventing perfectly stacked nodes.
        const occupied = Object.values(wf).flatMap((entry) => {
          if (!entry || typeof entry !== 'object') return [];
          const entryAttrs = (entry as NodeRec).__attributes__;
          if (!entryAttrs || typeof entryAttrs !== 'object') return [];
          const x = (entryAttrs as NodeRec).x;
          const y = (entryAttrs as NodeRec).y;
          return typeof x === 'number' && typeof y === 'number' ? [{ x, y }] : [];
        });
        let pos = position;
        const step = 24;
        for (let attempt = 0; attempt < occupied.length + 1; attempt += 1) {
          const candidate = attempt === 0
            ? position
            : { x: position.x + step * attempt, y: position.y + step * attempt };
          if (!occupied.some((point) => point.x === candidate.x && point.y === candidate.y)) {
            pos = candidate;
            break;
          }
        }
        node.__attributes__ = { ...attrs, x: pos.x, y: pos.y };
        wf[id] = node;
        return wf;
      }),

    addNodes: (payloads, positions) =>
      get().applyEdit((wf) => {
        const allocated = new Set(Object.keys(wf));
        payloads.forEach((payload, i) => {
          const id = nextNodeId(allocated);
          allocated.add(id);
          const node = applyNodeTypeDefaults(structuredClone(payload) as NodeRec);
          node.node_id = id;
          if (!node.node_name) node.node_name = id;
          const pos = positions?.[i];
          const attrs = (node.__attributes__ as NodeRec | undefined) ?? {};
          if (pos) {
            node.__attributes__ = { ...attrs, x: pos.x, y: pos.y };
          } else {
            node.__attributes__ = attrs;
          }
          wf[id] = node;
        });
        return wf;
      }),

    removeNode: (id) => get().removeNodes([id]),

    removeNodes: (ids) =>
      get().applyEdit((wf) => {
        const idSet = new Set(ids);
        for (const id of ids) delete wf[id];
        for (const key of Object.keys(wf)) {
          if (!isNodeKey(key)) continue;
          const node = asNode(wf, key);
          if (node) scrubReferencesToIds(node, idSet);
        }
        return wf;
      }),

    connectNodes: (source, target) =>
      get().applyEdit((wf) => {
        const node = asNode(wf, source);
        if (!node) return wf;
        // syncTypeConfigOnEdgeChange may reject (LoopBegin 2nd child).
        const ok = syncTypeConfigOnEdgeChange(wf, source, target, 'add');
        if (!ok) return wf;
        const children = getChildren(node);
        if (!children.includes(target)) children.push(target);
        return wf;
      }),

    disconnectNodes: (source, target) =>
      get().applyEdit((wf) => {
        const node = asNode(wf, source);
        if (!node) return wf;
        const children = getChildren(node);
        node.children = children.filter((c) => c !== target);
        syncTypeConfigOnEdgeChange(wf, source, target, 'remove');
        return wf;
      }),

    pairNodes: (aId, bId, kind) =>
      get().applyEdit((wf) => {
        const a = asNode(wf, aId);
        const b = asNode(wf, bId);
        if (!a || !b) return wf;
        const aType = nodeType(a);
        // Determine which side is the "start/begin" pointer-holder.
        let startId: string;
        let endId: string;
        let startPtr: string;
        let endPtr: string;
        if (kind === 'parallel') {
          startPtr = 'parallel_end_node_id';
          endPtr = 'parallel_start_node_id';
          if (aType === 'ParallelStartNode') {
            startId = aId;
            endId = bId;
          } else {
            startId = bId;
            endId = aId;
          }
        } else {
          startPtr = 'loop_end_node_id';
          endPtr = 'loop_begin_node_id';
          if (aType === 'LoopBeginNode') {
            startId = aId;
            endId = bId;
          } else {
            startId = bId;
            endId = aId;
          }
        }
        const startNode = asNode(wf, startId);
        const endNode = asNode(wf, endId);
        if (!startNode || !endNode) return wf;
        const startCfg = getConfig(startNode);
        const endCfg = getConfig(endNode);

        // Clear the OLD partner's back-pointer (don't orphan a stale ref).
        const prevEndId = startCfg[startPtr];
        if (typeof prevEndId === 'string' && prevEndId !== endId) {
          const prevEnd = asNode(wf, prevEndId);
          if (prevEnd) getConfig(prevEnd)[endPtr] = null;
        }
        const prevStartId = endCfg[endPtr];
        if (typeof prevStartId === 'string' && prevStartId !== startId) {
          const prevStart = asNode(wf, prevStartId);
          if (prevStart) getConfig(prevStart)[startPtr] = null;
        }

        startCfg[startPtr] = endId;
        endCfg[endPtr] = startId;
        return wf;
      }),

    copyNodes: (ids) =>
      set((state) => {
        if (state.draft === null) return state;
        const clipboard: NodeRec[] = [];
        for (const id of ids) {
          const node = asNode(state.draft, id);
          if (node) clipboard.push(structuredClone(node) as NodeRec);
        }
        return { clipboard };
      }),

    pasteNodes: (anchorPos) => {
      const { clipboard, addNodes } = get();
      if (clipboard.length === 0) return;
      const payloads = clipboard.map((node) => {
        const copy = structuredClone(node) as NodeRec;
        resetNodeTopology(copy);
        return copy;
      });
      // Offset each pasted node from the anchor so they don't stack.
      const positions = payloads.map((_, i) => ({
        x: anchorPos.x + i * 24,
        y: anchorPos.y + i * 24,
      }));
      addNodes(payloads, positions);
    },

    isDirty: () => {
      const { draft, baseline } = get();
      return JSON.stringify(draft) !== baseline;
    },

    markSaved: (version, savedDraft) =>
      set((state) => ({
        baseline: JSON.stringify(savedDraft ?? state.draft),
        baseVersion: version ?? state.baseVersion,
        dirty: JSON.stringify(state.draft) !== JSON.stringify(savedDraft ?? state.draft),
      })),

    markClean: () =>
      set((state) => ({
        baseline: JSON.stringify(state.draft),
        dirty: false,
      })),

    undo: () =>
      set((state) => {
        if (state.undoStack.length === 0 || state.draft === null) return state;
        const undoStack = state.undoStack.slice();
        const prev = undoStack.pop() as string;
        const currentSnapshot = JSON.stringify(state.draft);
        const redoStack = [...state.redoStack, currentSnapshot];
        return {
          draft: JSON.parse(prev) as WorkflowDraft,
          // dirty re-derives: undoing back to baseline bytes = clean.
          dirty: prev !== state.baseline,
          undoStack,
          redoStack,
        };
      }),

    redo: () =>
      set((state) => {
        if (state.redoStack.length === 0 || state.draft === null) return state;
        const redoStack = state.redoStack.slice();
        const next = redoStack.pop() as string;
        const currentSnapshot = JSON.stringify(state.draft);
        const undoStack = [...state.undoStack, currentSnapshot];
        return {
          draft: JSON.parse(next) as WorkflowDraft,
          dirty: next !== state.baseline,
          undoStack,
          redoStack,
        };
      }),
  })),
);
