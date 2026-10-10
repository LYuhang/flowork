// @vitest-environment node
import { describe, expect, it } from 'vitest';
import ELK from 'elkjs/lib/elk.bundled';
import { calculateCompactLayout, compactLayoutGraph, type LayoutNode, type LayoutEdge } from '../compact-layout';

function chain(count: number) {
  const nodes: LayoutNode[] = Array.from({ length: count }, (_, i) => ({
    id: `n${i}`, width: 224, height: i === 4 ? 360 : 140,
    ports: [{ id: 'in', x: 0, y: 50, side: 'WEST' }, { id: 'out', x: 224, y: 50, side: 'EAST' }],
  }));
  const edges: LayoutEdge[] = nodes.slice(1).map((n, i) => ({ id: `e${i}`, source: nodes[i].id, target: n.id, sourcePort: 'out', targetPort: 'in' }));
  return { nodes, edges };
}

describe('compact layout using the production ELK engine', () => {
  it('keeps a small graph in one row when it fits without shrinking', async () => {
    const { nodes, edges } = chain(3);
    const result = await calculateCompactLayout(new ELK(), nodes, edges, { width: 900, height: 700 });
    expect(new Set(Object.values(result.positions).map(p => p.y)).size).toBe(1);
    expect(result.positions.n2.x).toBeGreaterThan(result.positions.n1.x);
  });

  it('wraps a long chain, preserving measured dimensions and actual handle positions', async () => {
    const { nodes, edges } = chain(40);
    const before = structuredClone(nodes);
    const result = await calculateCompactLayout(new ELK(), nodes, edges, { width: 1000, height: 650 });
    expect(result.width).toBeLessThan(4000);
    expect(result.height).toBeGreaterThan(360);
    expect(Object.keys(result.routes)).toHaveLength(39);
    for (const edge of edges) {
      const points = result.routes[edge.id];
      expect(points[0]).toEqual({ x: result.positions[edge.source].x + 224, y: result.positions[edge.source].y + 50 });
      expect(points.at(-1)).toEqual({ x: result.positions[edge.target].x, y: result.positions[edge.target].y + 50 });
    }
    expect(nodes).toEqual(before);
  });

  it('preserves handle endpoints extending beyond the node border', async () => {
    const { nodes, edges } = chain(2);
    for (const node of nodes) {
      node.ports[0].x = -3;
      node.ports[1].x = 227;
    }
    const result = await calculateCompactLayout(new ELK(), nodes, edges, { width: 1000, height: 600 });
    expect(result.routes.e0[0].x).toBe(result.positions.n0.x + 227);
    expect(result.routes.e0.at(-1)!.x).toBe(result.positions.n1.x - 3);
  });

  it('does not collide port names between nodes or lose pairing edges', () => {
    const { nodes, edges } = chain(2);
    const graph = compactLayoutGraph(nodes, [...edges, { ...edges[0], id: 'back', source: 'n1', target: 'n0', pairing: true }], 1.6, true);
    const ports = graph.children!.flatMap(n => n.ports!.map(p => p.id));
    expect(new Set(ports).size).toBe(4);
    expect(graph.edges).toHaveLength(2);
    expect(graph.edges![1].layoutOptions!['elk.layered.priority.direction']).toBe('0');
  });

  it('returns an empty layout without invoking the engine', async () => {
    const engine = { layout: () => { throw Error('must not run'); } };
    expect(await calculateCompactLayout(engine, [], [], { width: 0, height: 0 })).toEqual({ width: 0, height: 0, positions: {}, routes: {} });
  });

  it('reports engine failures without retrying or replacing the graph', async () => {
    const { nodes, edges } = chain(2);
    let calls = 0;
    const engine = { layout: async () => { calls++; throw Error('layout failed'); } };
    await expect(calculateCompactLayout(engine, nodes, edges, { width: 1000, height: 600 })).rejects.toThrow('layout failed');
    expect(calls).toBe(1);
  });
});
