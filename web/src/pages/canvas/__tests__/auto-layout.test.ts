import { describe, expect, it } from 'vitest';
import type { Edge, Node } from '@xyflow/react';
import { autoLayout, layoutOverlappingNodes } from '@/pages/canvas/auto-layout';

describe('incoming saved graph coordinates', () => {
  it('repairs an inserted node inside an existing card, preserving identity and selection', () => {
    const nodes: Node[] = [0, 130, 280].map((x, i) => ({
      id: String(i), position: { x, y: 0 }, data: {}, selected: i === 0,
      measured: { width: 224, height: 136 },
    }));
    const before = structuredClone(nodes);
    const out = layoutOverlappingNodes(nodes, [
      { id: 'a', source: '0', target: '1' }, { id: 'b', source: '1', target: '2' },
    ]);
    expect(out[1].position.x).toBeGreaterThanOrEqual(out[0].position.x + 224);
    expect(out[2].position.x).toBeGreaterThanOrEqual(out[1].position.x + 224);
    expect(out[0].selected).toBe(true);
    expect(nodes).toEqual(before);
  });

  it('keeps non-overlapping manual positions intact', () => {
    const nodes: Node[] = [0, 500].map((x, i) => ({ id: String(i), position: { x, y: 80 }, data: {} }));
    expect(layoutOverlappingNodes(nodes, [])).toBe(nodes);
  });
});

describe('autoLayout (left-to-right)', () => {
  it('lays a 2-node chain A->B horizontally (B to the right of A)', () => {
    const nodes: Node[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: {} },
      { id: 'b', position: { x: 0, y: 0 }, data: {} },
    ];
    const edges: Edge[] = [{ id: 'e', source: 'a', target: 'b' }];
    const out = autoLayout(nodes, edges);
    const a = out.find((n) => n.id === 'a')!;
    const b = out.find((n) => n.id === 'b')!;
    // LR: the downstream node advances along X, not Y.
    expect(b.position.x).toBeGreaterThan(a.position.x);
    expect(Math.abs(b.position.y - a.position.y)).toBeLessThan(Math.abs(b.position.x - a.position.x));
  });

  it('places parallel siblings on the same rank (same x), spread along y', () => {
    const nodes: Node[] = [
      { id: 'root', position: { x: 0, y: 0 }, data: {} },
      { id: 's1', position: { x: 0, y: 0 }, data: {} },
      { id: 's2', position: { x: 0, y: 0 }, data: {} },
    ];
    const edges: Edge[] = [
      { id: 'e1', source: 'root', target: 's1' },
      { id: 'e2', source: 'root', target: 's2' },
    ];
    const out = autoLayout(nodes, edges);
    const s1 = out.find((n) => n.id === 's1')!;
    const s2 = out.find((n) => n.id === 's2')!;
    // siblings share a rank → same x, different y (vertical fan-out, horizontal flow).
    expect(s1.position.x).toBeCloseTo(s2.position.x, 0);
    expect(s1.position.y).not.toBeCloseTo(s2.position.y, 0);
  });
});


describe('expanded cards and loop routing', () => {
  it('keeps expanded sibling cards apart without changing input positions', () => {
    const nodes: Node[] = ['root', 'a', 'b'].map((id) => ({
      id, position: { x: 0, y: 0 }, data: {}, measured: { width: 224, height: id === 'a' ? 480 : 300 },
    }));
    const edges: Edge[] = ['a', 'b'].map((target) => ({ id: target, source: 'root', target }));
    const out = autoLayout(nodes, edges);
    for (const a of out) for (const b of out) {
      if (a.id === b.id) continue;
      const overlapX = Math.min(a.position.x + a.measured!.width!, b.position.x + b.measured!.width!) - Math.max(a.position.x, b.position.x);
      const overlapY = Math.min(a.position.y + a.measured!.height!, b.position.y + b.measured!.height!) - Math.max(a.position.y, b.position.y);
      expect(overlapX <= 0 || overlapY <= 0).toBe(true);
    }
    expect(nodes.every((node) => node.position.x === 0 && node.position.y === 0)).toBe(true);
  });

  it('does not let a visual loop-back edge reorder the forward chain', () => {
    const nodes: Node[] = ['begin', 'body', 'end'].map((id) => ({ id, data: {}, position: { x: 0, y: 0 } }));
    const edges: Edge[] = [
      { id: 'a', source: 'begin', target: 'body' },
      { id: 'b', source: 'body', target: 'end' },
    ];
    expect(autoLayout(nodes, [...edges, { id: 'back', source: 'end', target: 'begin', data: { __pairing__: true } }]))
      .toEqual(autoLayout(nodes, edges));
  });
});
