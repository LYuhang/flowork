import type { components } from '@/lib/api/schema';

type Attachment = components['schemas']['ResourceContextAttachment'];
/** Persist business endpoints, never React Flow's transient edge identity. */
export function workflowReference(workflowId: string, version: string, label: string,
  nodeIds: string[] = [], edges: { source: string; target: string }[] = []): Attachment {
  const nodes = [...new Set(nodeIds)].sort();
  const connections = [...new Map(edges.map(edge => [JSON.stringify([edge.source, edge.target]), { source: edge.source, target: edge.target }])).values()]
    .sort((a, b) => a.source.localeCompare(b.source) || a.target.localeCompare(b.target));
  return { schema_version: 1, id: crypto.randomUUID(), type: 'resource', label,
    resource: { kind: 'workflow', workflow_id: workflowId, version },
    ...(nodes.length || connections.length ? { selector: { kind: 'workflow_elements', node_ids: nodes, edges: connections } } : {}) };
}

export type WorkflowFocus = { nodeIds: string[]; edges: { source: string; target: string }[] };
export function parseWorkflowFocus(value: string | null): WorkflowFocus | null {
  if (!value || value.length > 65536) return null;
  try {
    const data = JSON.parse(value);
    const id = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512;
    if (!data || !Array.isArray(data.nodeIds) || !Array.isArray(data.edges)
      || data.nodeIds.length > 100 || data.edges.length > 100 || !data.nodeIds.every(id)
      || !data.edges.every((edge: { source?: unknown; target?: unknown } | null) => edge && id(edge.source) && id(edge.target))) return null;
    return { nodeIds: data.nodeIds, edges: data.edges.map((edge: {source: string; target: string}) => ({ source: edge.source, target: edge.target })) };
  } catch { return null; }
}
