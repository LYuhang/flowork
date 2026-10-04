/**
 * Dagre-powered auto-layout helper.
 *
 * Pure function — given a list of xyflow `Node`/`Edge` pairs, compute a
 * left-to-right layered layout and return a fresh `nodes` array with new
 * `position` values. Edges are untouched (xyflow re-routes them based on
 * source/target positions). `rankdir: 'LR'` (horizontal) matches the
 * node-graph / LLM-workflow convention (n8n / LangFlow / Coze / Dify) and
 * reads better for the branch/parallel fan-out this canvas supports; the
 * `CustomNode` handles are correspondingly Left (target) / Right (source).
 *
 * Use measured node dimensions, including expanded output/approval panels.
 * Unmounted nodes use a compact fallback until React Flow measures them.
 */
import dagre from 'dagre';
import type { Edge, Node } from '@xyflow/react';
import { workflowDictToNodesEdges } from './WorkflowGraph';

const NODE_WIDTH = 224;
const NODE_HEIGHT = 140;

/** Repair overlapping saved coordinates without rewriting the persisted graph. */
export function layoutOverlappingNodes(nodes: Node[], edges: Edge[]): Node[] {
  const overlaps = nodes.some((a, i) => nodes.slice(i + 1).some((b) => {
    const aw = a.measured?.width ?? a.width ?? NODE_WIDTH;
    const ah = a.measured?.height ?? a.height ?? NODE_HEIGHT;
    const bw = b.measured?.width ?? b.width ?? NODE_WIDTH;
    const bh = b.measured?.height ?? b.height ?? NODE_HEIGHT;
    return a.position.x < b.position.x + bw && b.position.x < a.position.x + aw
      && a.position.y < b.position.y + bh && b.position.y < a.position.y + ah;
  }));
  return overlaps ? autoLayout(nodes, edges) : nodes;
}

export function autoLayout(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 60, ranksep: 80 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, {
    width: n.measured?.width ?? n.width ?? NODE_WIDTH,
    height: n.measured?.height ?? n.height ?? NODE_HEIGHT,
  });
  // Loop pairing lines visualize control flow back to the beginning; they
  // must not turn the forward layout graph into a cycle.
  for (const e of edges) {
    if (!e.data?.__pairing__ && g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }
  dagre.layout(g);
  return nodes.map((n) => {
    const { x, y, width, height } = g.node(n.id);
    return { ...n, position: { x: x - width / 2, y: y - height / 2 } };
  });
}

/**
 * Apply dagre auto-layout to a flat workflow dict IN PLACE, writing the
 * computed `{x, y}` back into each node's `__attributes__`. Used by:
 *   - the "Tidy up" toolbar action (re-arrange every node), and
 *   - the JSON-upload mutator (`onlyPositionless: true` — lay out just the
 *     freshly-loaded nodes that have no position, so existing layout is kept).
 *
 * Operates on the SAME dict the caller passes (the `applyEdit` clone), so the
 * mutation is captured in one undo step. Reserved `__…__` keys are ignored by
 * `workflowDictToNodesEdges`. Returns the dict for chaining.
 */
export function layoutWorkflowDict(
  wf: Record<string, unknown>,
  opts: { onlyPositionless?: boolean; measuredNodes?: Node[] } = {},
): Record<string, unknown> {
  const { nodes, edges } = workflowDictToNodesEdges(wf);
  const measured = new Map(opts.measuredNodes?.map((node) => [node.id, node.measured]));
  const laid = autoLayout(nodes.map((node) => ({ ...node, measured: measured.get(node.id) })), edges);
  for (const n of laid) {
    const node = wf[n.id] as Record<string, unknown> | undefined;
    if (!node || typeof node !== 'object') continue;
    const attrs = (node.__attributes__ as Record<string, unknown> | undefined) ?? {};
    if (opts.onlyPositionless && hasPosition(attrs)) continue;
    node.__attributes__ = { ...attrs, x: n.position.x, y: n.position.y };
  }
  return wf;
}

function hasPosition(attrs: Record<string, unknown>): boolean {
  return typeof attrs.x === 'number' && typeof attrs.y === 'number';
}
