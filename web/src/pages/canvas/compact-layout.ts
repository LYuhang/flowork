import type { ELK, ElkNode, ElkPoint } from 'elkjs/lib/elk-api';

export type LayoutPort = ElkPoint & { id: string; side: 'WEST' | 'EAST' | 'SOUTH' };
export type LayoutNode = { id: string; width: number; height: number; ports: LayoutPort[] };
export type LayoutEdge = { id: string; source: string; target: string; sourcePort: string; targetPort: string; pairing?: boolean };
export type CompactLayout = {
  width: number;
  height: number;
  positions: Record<string, ElkPoint>;
  routes: Record<string, ElkPoint[]>;
};

// Include the node ID: handle names are only unique within a node.
const portId = (node: string, port: string) => JSON.stringify([node, port]);

export function compactLayoutGraph(nodes: LayoutNode[], edges: LayoutEdge[], aspect: number, wrap: boolean): ElkNode {
  return {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.aspectRatio': String(aspect),
      'elk.layered.wrapping.strategy': wrap ? 'MULTI_EDGE' : 'OFF',
      'elk.spacing.nodeNode': '60',
      'elk.layered.spacing.nodeNodeBetweenLayers': '80',
    },
    children: nodes.map(node => ({
      id: node.id, width: node.width, height: node.height,
      layoutOptions: { 'elk.portConstraints': 'FIXED_POS' },
      ports: node.ports.map(port => ({
        id: portId(node.id, port.id), x: port.x, y: port.y, width: 0, height: 0,
        layoutOptions: {
          'elk.port.side': port.side,
          // FIXED_POS alone snaps a port to the node border; handles extend beyond it.
          'elk.port.borderOffset': String(port.side === 'WEST' ? -port.x : port.side === 'EAST' ? port.x - node.width : port.y - node.height),
        },
      })),
    })),
    edges: edges.map(edge => ({
      id: edge.id,
      sources: [portId(edge.source, edge.sourcePort)],
      targets: [portId(edge.target, edge.targetPort)],
      layoutOptions: { 'elk.layered.priority.direction': edge.pairing ? '0' : '10' },
    })),
  };
}

function extractLayout(graph: ElkNode): CompactLayout {
  const positions: CompactLayout['positions'] = {};
  const routes: CompactLayout['routes'] = {};
  for (const node of graph.children ?? []) {
    if (node.x === undefined || node.y === undefined) throw new Error(`Layout missing position: ${node.id}`);
    positions[node.id] = { x: node.x, y: node.y };
  }
  for (const edge of graph.edges ?? []) {
    // Flat, two-endpoint graph edges have one section. Never silently discard a route.
    if (edge.sections?.length !== 1) throw new Error(`Layout missing continuous route: ${edge.id}`);
    const section = edge.sections[0];
    routes[edge.id] = [section.startPoint, ...(section.bendPoints ?? []), section.endPoint];
  }
  return { width: graph.width ?? 0, height: graph.height ?? 0, positions, routes };
}

/** Pure orchestration: caller owns the worker and applying the result as one edit. */
export async function calculateCompactLayout(
  engine: Pick<ELK, 'layout'>,
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  viewport: { width: number; height: number },
): Promise<CompactLayout> {
  if (nodes.length === 0) return { width: 0, height: 0, positions: {}, routes: {} };
  if (viewport.width <= 0 || viewport.height <= 0) throw new Error('Canvas has no available layout area');
  const aspect = viewport.width / viewport.height;
  const straight = extractLayout(await engine.layout(compactLayoutGraph(nodes, edges, aspect, false)));
  // A small graph already readable at native scale should keep its simple shape.
  const fit = (layout: CompactLayout) => Math.min(1, viewport.width / layout.width, viewport.height / layout.height);
  if (fit(straight) === 1) return straight;
  const wrapped = extractLayout(await engine.layout(compactLayoutGraph(nodes, edges, aspect, true)));
  return fit(wrapped) > fit(straight) ? wrapped : straight;
}
