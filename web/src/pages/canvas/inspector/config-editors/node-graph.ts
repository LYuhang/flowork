/**
 * Graph-aware helpers for the interactive node config editors (Stream 3).
 *
 * The config editors receive only `{ config, readOnly, onChange }` from
 * `NodeTab` (owned by Stream 5) — but Stream-3 editors must:
 *   - render node-reference fields as DROPDOWNS labeled by `node_name`
 *     (next_node_id from the current node's children; pairing pointers
 *     from candidate nodes of the partner type);
 *   - call the edit store's `pairNodes` for auto-pairing.
 *
 * Rather than widen the `NodeConfigEditorProps` contract (which would
 * force a change to the Stream-5-owned `NodeTab`), the editors source the
 * two extra things themselves:
 *   - the SELECTED node id from xyflow (`useSelectedNodeId`), the same
 *     source `NodeTab` uses (`useNodes().find(selected)`);
 *   - the draft graph from the edit store (`useDraftGraph`).
 *
 * Both are read-only views; every WRITE still flows through the editor's
 * `onChange` (config) or the store's `pairNodes` (pairing) — honouring
 * the 0b state-ownership rule (no authoritative local state).
 */
import { useStore } from '@xyflow/react';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useWorkflowSnapshot } from '@/pages/canvas/WorkflowSnapshotContext';

export interface NodeRef {
  /** node_id, e.g. "node_3". */
  id: string;
  /** user-facing label (node_name, falling back to id). */
  name: string;
  type?: string;
}

interface RawNode {
  node_id?: string;
  node_name?: string;
  node_type?: string;
  children?: unknown;
  input_fields?: Record<string, unknown>;
  output_fields?: Record<string, unknown>;
  node_config?: Record<string, unknown>;
}

/** The currently-selected node id (xyflow selection), or null. */
export function useSelectedNodeId(): string | null {
  return useStore((state) => state.nodes.find((node) => node.selected)?.id ?? null);
}

/** The live draft graph (flat dict keyed by node_id), or null. */
export function useDraftGraph(): Record<string, unknown> | null {
  const snapshot = useWorkflowSnapshot();
  return useWorkflowEditStore((s) => snapshot ?? s.draft);
}

function isNodeKey(key: string): boolean {
  return !key.startsWith('__');
}

/** Label a node for a dropdown: `node_name` (id) when they differ. */
export function labelOf(node: RawNode | null | undefined, id: string): string {
  const name = node?.node_name;
  if (typeof name === 'string' && name && name !== id) return `${name} (${id})`;
  return id;
}

/** All nodes in the graph as `{id,name,type}`, excluding reserved keys. */
export function listNodes(graph: Record<string, unknown> | null): NodeRef[] {
  if (!graph) return [];
  const out: NodeRef[] = [];
  for (const [id, raw] of Object.entries(graph)) {
    if (!isNodeKey(id)) continue;
    const node = raw as RawNode | null;
    out.push({ id, name: labelOf(node, id), type: node?.node_type });
  }
  return out;
}

/** Candidate nodes of a given `node_type` (for pairing dropdowns). */
export function listNodesOfType(
  graph: Record<string, unknown> | null,
  type: string,
): NodeRef[] {
  return listNodes(graph).filter((n) => n.type === type);
}

/** The children of `nodeId` as `{id,name}` refs (for next_node_id dropdowns). */
export function childrenOf(
  graph: Record<string, unknown> | null,
  nodeId: string | null,
): NodeRef[] {
  if (!graph || !nodeId) return [];
  const node = graph[nodeId] as RawNode | null;
  const children = Array.isArray(node?.children)
    ? (node?.children as string[])
    : [];
  return children.map((id) => ({
    id,
    name: labelOf(graph[id] as RawNode | null, id),
    type: (graph[id] as RawNode | null)?.node_type,
  }));
}

/** Read a node's input-field names (the `{field}` placeholders a Condition can use). */
export function inputFieldNames(
  graph: Record<string, unknown> | null,
  nodeId: string | null,
): string[] {
  if (!graph || !nodeId) return [];
  const node = graph[nodeId] as RawNode | null;
  const fields = node?.input_fields;
  return fields && typeof fields === 'object' ? Object.keys(fields) : [];
}

/** The single StartNode's input-field names (a sensible Condition fallback). */
export function startInputFieldNames(
  graph: Record<string, unknown> | null,
): string[] {
  if (!graph) return [];
  for (const [id, raw] of Object.entries(graph)) {
    if (!isNodeKey(id)) continue;
    const node = raw as RawNode | null;
    if (node?.node_type === 'StartNode') {
      const fields = node.input_fields;
      return fields && typeof fields === 'object' ? Object.keys(fields) : [];
    }
  }
  return [];
}
