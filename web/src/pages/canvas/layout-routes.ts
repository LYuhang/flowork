import type { Edge, Node, XYPosition } from '@xyflow/react';
import type { CompactLayout } from './compact-layout';

export type LayoutGeometry = { id: string; x: number; y: number; width: number; height: number }[];
export type SavedCanvasLayout = {
  version: 1;
  nodes: LayoutGeometry;
  edges: { id: string; source: string; target: string; sourceHandle?: string | null; targetHandle?: string | null; points: XYPosition[] }[];
};

/** Persist display coordinates and paths together, inside the caller's single undo edit. */
export function saveCompactLayout(wf: Record<string, unknown>, nodes: Node[], edges: Edge[], layout: CompactLayout) {
  const geometry: LayoutGeometry = [];
  for (const node of nodes) {
    const position = layout.positions[node.id];
    const payload = wf[node.id] as Record<string, unknown>;
    payload.__attributes__ = { ...(payload.__attributes__ as object), ...position };
    geometry.push({ id: node.id, ...position, width: node.measured?.width ?? node.width ?? 224, height: node.measured?.height ?? node.height ?? 140 });
  }
  const saved: SavedCanvasLayout = {
    version: 1, nodes: geometry,
    edges: edges.map(({ id, source, target, sourceHandle, targetHandle }) => ({ id, source, target, sourceHandle, targetHandle, points: layout.routes[id] })),
  };
  wf.__meta__ = { ...(wf.__meta__ as object), canvas_layout: saved };
  return wf;
}

export function layoutGeometryMatches(expected: LayoutGeometry, nodes: Node[], measured = false): boolean {
  if (nodes.length !== expected.length || new Set(expected.map(box => box.id)).size !== expected.length) return false;
  const byId = new Map(nodes.map(node => [node.id, node]));
  return expected.every(box => {
    const node = byId.get(box.id);
    return node !== undefined && node.position.x === box.x && node.position.y === box.y
      && (!measured || (node.measured?.width === box.width && node.measured?.height === box.height));
  });
}

const point = (value: unknown): value is XYPosition => {
  const p = value as XYPosition | null;
  return p !== null && typeof p === 'object' && Number.isFinite(p.x) && Number.isFinite(p.y);
};

/** JSON imports can carry arbitrary metadata; ignore an invalid/stale display cache. */
export function readLayoutRoutes(wf: Record<string, unknown>, nodes: Node[], edges: Edge[]): SavedCanvasLayout | null {
  const saved = (wf.__meta__ as { canvas_layout?: SavedCanvasLayout } | undefined)?.canvas_layout;
  if (!saved || saved.version !== 1 || !Array.isArray(saved.nodes) || !Array.isArray(saved.edges)) return null;
  if (!saved.nodes.every(n => point(n) && typeof n.id === 'string' && Number.isFinite(n.width) && Number.isFinite(n.height))) return null;
  if (!layoutGeometryMatches(saved.nodes, nodes) || saved.edges.length !== edges.length) return null;
  const byId = new Map(edges.map(edge => [edge.id, edge]));
  if (new Set(saved.edges.map(edge => edge?.id)).size !== edges.length) return null;
  for (const route of saved.edges) {
    const edge = byId.get(route?.id);
    if (!edge || edge.source !== route.source || edge.target !== route.target
      || (edge.sourceHandle ?? null) !== (route.sourceHandle ?? null)
      || (edge.targetHandle ?? null) !== (route.targetHandle ?? null)
      || !Array.isArray(route.points) || route.points.length < 2 || !route.points.every(point)) return null;
  }
  return saved;
}

/** Put branch labels on the longest segment, rather than on a bend. */
export function routedPath(points: XYPosition[]): { path: string; labelX: number; labelY: number } {
  let longest = -1;
  let labelX = points[0].x;
  let labelY = points[0].y;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i];
    const distance = Math.hypot(b.x - a.x, b.y - a.y);
    if (distance > longest) { longest = distance; labelX = (a.x + b.x) / 2; labelY = (a.y + b.y) / 2; }
  }
  return { path: points.map((p, i) => `${i ? 'L' : 'M'} ${p.x} ${p.y}`).join(' '), labelX, labelY };
}
