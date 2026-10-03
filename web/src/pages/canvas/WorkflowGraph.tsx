/* eslint-disable react-refresh/only-export-components -- Shared canvas exports its pure, tested graph projection alongside the render boundary. */
/** Shared workflow projection and canvas presentation, independent of editor state. */
import type { ComponentProps } from 'react';
import { Background, Controls, MiniMap, ReactFlow, type Node, type Edge } from '@xyflow/react';
import {
  CustomNode,
  LOOP_BACK_SOURCE_HANDLE_ID,
  LOOP_BACK_TARGET_HANDLE_ID,
  type NodePayload,
} from './nodes/CustomNode';
import { nodeWarnings } from './nodeWarnings';

const nodeTypes = { custom: CustomNode };

export function WorkflowGraph({ showMiniMap = true, showControls = true, showInteractiveControls = true, children, ...props }: ComponentProps<typeof ReactFlow> & { showMiniMap?: boolean; showControls?: boolean; showInteractiveControls?: boolean }) {
  return (
    <ReactFlow
      className="workflow-canvas [&_.react-flow__edge-text]:pointer-events-none [&_.react-flow__edge-textbg]:pointer-events-none"
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.16, minZoom: 0.1, maxZoom: 1 }}
      minZoom={0.1}
      zoomOnDoubleClick={false}
      {...props}
    >
      {children}
      <Background gap={20} />
      {showControls && <Controls
        showInteractive={showInteractiveControls}
        position="bottom-left"
        className="!overflow-hidden !rounded-md !border !border-edge-structural !bg-surface-raised !shadow-none"
      />}
      {showMiniMap && <MiniMap position="bottom-right" pannable zoomable
        className="!hidden !rounded-md !border !border-edge-structural !bg-surface-sunken !shadow-none lg:!block" />}
    </ReactFlow>
  );
}

/** Semantic focus color for config-derived Parallel/Loop pairing edges. */
const PAIRING_EDGE_COLOR = 'oklch(var(--focus))';

/** Pure: flat-dict workflow → xyflow nodes/edges. Exported for tests later. */
export function workflowDictToNodesEdges(
  wf: Record<string, unknown> | null,
): { nodes: Node[]; edges: Edge[] } {
  if (!wf) return { nodes: [], edges: [] };

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Cheap LOCAL validity warnings (empty condition_str, conditions≠children,
  // unpaired Parallel/Loop, dangling ref). Computed once here so each
  // CustomNode receives its own `__warnings__` (string[] of i18n keys) via
  // `data` — no extra context plumbing. See `nodeWarnings.ts`.
  const warnings = nodeWarnings(wf as Record<string, unknown>);

  for (const [key, value] of Object.entries(wf)) {
    if (key.startsWith('__')) continue;
    const payload = (value ?? {}) as NodePayload;
    const attrs = payload.__attributes__ ?? {};
    nodes.push({
      id: key,
      type: 'custom',
      position: { x: attrs.x ?? 0, y: attrs.y ?? 0 },
      data: {
        ...(payload as unknown as Record<string, unknown>),
        __warnings__: warnings.get(key) ?? [],
      },
    });
    const children = Array.isArray(payload.children) ? payload.children : [];
    for (const child of children) {
      edges.push({
        id: `${key}->${child}`,
        source: key,
        target: child,
        animated: true,
        label: edgeLabelFor(payload, child),
      });
    }

    // Pairing edge (Stream 8 A1): when a Parallel/Loop START holds a set
    // partner pointer, emit an ADDITIONAL distinct dashed/labeled edge to its
    // END so the pairing is VISIBLE on canvas though it is NOT a child edge.
    // These are config-derived: non-deletable + non-selectable so they cannot
    // be confused with (or mutate) the `children` graph.
    const pairing = pairingEdgeFor(key, payload);
    if (pairing) edges.push(pairing);
  }

  return { nodes, edges };
}

/**
 * Build the config-derived pairing edge for a Loop BEGIN node, or `null`.
 *
 * ONLY loops draw a pairing edge: the Loop's begin↔end pairing carries an
 * implicit *jump-back* (the end node loops control flow to the begin node), so
 * the dashed back-edge is meaningful. Parallel start/end are paired the same
 * way in config, but there is no jump — the partner is reached through the
 * ordinary `children` graph — so a pairing edge there is just visual noise and
 * is intentionally NOT emitted.
 *
 * Derived from the BEGIN side's partner pointer (`loop_end_node_id`) so we
 * never emit it twice and it is re-derived from config on every render (moving
 * or selecting nodes can't break it). The edge is flagged
 * `data.__pairing__: true` (a non-`children` marker the canvas uses to keep it
 * out of disconnect handling) and is non-deletable + non-selectable.
 *
 * Routing: the edge represents the loop-back JUMP, so it runs from the BOTTOM
 * of the LoopEnd node (`source`) to the BOTTOM of the LoopBegin node
 * (`target`) — i.e. source/target are flipped to end→begin vs the config
 * pointer — attaching to the DEDICATED, non-connectable bottom handles
 * (`loop-back-source` / `loop-back-target`, rendered only on Loop boundaries by
 * `CustomNode`). A `smoothstep` path between two BOTTOM handles routes with
 * right-angles that dip DOWNWARD and back up, so the back-edge sits under the
 * node row and never hides beneath the left→right data flow. (smoothstep is the
 * cleanest of the built-ins here: two bottom anchors make it drop below both
 * nodes; a plain bezier would bow but can still clip a node sitting between
 * them, so we prefer the orthogonal dip. Best-effort — extreme layouts may
 * still graze a node, but it no longer overlaps the begin/end pair.)
 */
function pairingEdgeFor(id: string, payload: NodePayload): Edge | null {
  const cfg = payload.node_config as Record<string, unknown> | undefined;
  if (!cfg) return null;
  if (payload.node_type !== 'LoopBeginNode') return null;
  const endId: unknown = cfg.loop_end_node_id;
  const label = 'loop';
  if (typeof endId !== 'string' || !endId) return null;
  return {
    // id keeps the begin→end namespace (one stable edge per pair) even though
    // the rendered direction is end→begin.
    id: `pair:${id}->${endId}`,
    source: endId,
    target: id,
    sourceHandle: LOOP_BACK_SOURCE_HANDLE_ID,
    targetHandle: LOOP_BACK_TARGET_HANDLE_ID,
    type: 'smoothstep',
    label,
    animated: false,
    selectable: false,
    deletable: false,
    data: { __pairing__: true },
    style: {
      stroke: PAIRING_EDGE_COLOR,
      strokeDasharray: '6 4',
      strokeWidth: 1.5,
    },
    labelStyle: { fill: PAIRING_EDGE_COLOR, fontWeight: 600 },
  };
}

/**
 * Label an out-edge with its branch name for a Condition/ParallelStart
 * source so multiple branches are distinguishable on the canvas. Matched
 * by `next_node_id === child` (rename-safe), never by name. Returns
 * `undefined` for ordinary sources (xyflow omits the label).
 */
function edgeLabelFor(
  payload: NodePayload,
  child: string,
): string | undefined {
  const cfg = payload.node_config as Record<string, unknown> | undefined;
  if (!cfg) return undefined;
  if (payload.node_type === 'ConditionNode' && Array.isArray(cfg.conditions)) {
    const row = (cfg.conditions as Record<string, unknown>[]).find(
      (c) => c.next_node_id === child,
    );
    return row?.condition_name as string | undefined;
  }
  if (
    payload.node_type === 'ParallelStartNode' &&
    cfg.branches &&
    typeof cfg.branches === 'object'
  ) {
    for (const [name, b] of Object.entries(
      cfg.branches as Record<string, Record<string, unknown>>,
    )) {
      if (b.next_node_id === child) return name;
    }
  }
  return undefined;
}
