import { describe, expect, it } from 'vitest';
import type { Edge, Node } from '@xyflow/react';
import { reconcileCanvasNodes, reconcileCanvasEdges } from '../reconcile-graph';

const graph = (count: number): Node[] => Array.from({ length: count }, (_, i) => ({
  id: String(i), type: 'custom', position: { x: i * 300, y: 0 },
  data: { prompt: 'original', __warnings__: [] },
}));

describe('canvas graph reconciliation', () => {
  it('changes only one of 1000 node references for a cloned single-node edit and undo', () => {
    const previous = reconcileCanvasNodes([], graph(1000), [], false);
    const incoming = structuredClone(previous);
    incoming[500].data.prompt = 'edited';
    const next = reconcileCanvasNodes(previous, incoming, [], false);
    expect(next.filter((node, i) => node !== previous[i])).toHaveLength(1);
    expect(next[500].data.prompt).toBe('edited');
    const undone = reconcileCanvasNodes(next, structuredClone(previous), [], false);
    expect(undone.filter((node, i) => node !== next[i])).toHaveLength(1);
    expect(undone[500].data.prompt).toBe('original');
    expect(reconcileCanvasNodes(undone, structuredClone(undone), [], false)).toBe(undone);
  });

  it('retains transient UI state, removes deleted nodes and preserves keyed reordering', () => {
    const previous = graph(3);
    previous[1] = { ...previous[1], selected: true, dragging: true, measured: { width: 240, height: 150 } };
    const incoming = [structuredClone(previous[1]), structuredClone(previous[0])];
    incoming[0].selected = false;
    incoming[0].dragging = false;
    const next = reconcileCanvasNodes(previous, incoming, [], false);
    expect(next).toHaveLength(2);
    expect(next[0]).toBe(previous[1]);
    expect(next[0].measured).toBe(previous[1].measured);
    expect(next[1]).toEqual(previous[0]);
  });

  it('applies changed positions and warnings without retaining stale data', () => {
    const previous = graph(2);
    const incoming = structuredClone(previous);
    incoming[0].position = { x: 100, y: 100 };
    incoming[1].data.__warnings__ = ['missing child'];
    const next = reconcileCanvasNodes(previous, incoming, [], false);
    expect(next[0].position).toEqual({ x: 100, y: 100 });
    expect(next[1].data.__warnings__).toEqual(['missing child']);
  });

  it('repairs overlapping clean snapshots but preserves dirty coordinates', () => {
    const incoming = graph(2);
    incoming[1].position = { ...incoming[0].position };
    const clean = reconcileCanvasNodes([], incoming, [], true);
    expect(clean[0].position).not.toEqual(clean[1].position);
    expect(reconcileCanvasNodes([], incoming, [], false)).toEqual(incoming);
  });

  it('retains unchanged edges, selection and pairing metadata, updating labels and deletion', () => {
    const previous: Edge[] = [
      { id: 'a', source: '0', target: '1', label: 'yes', selected: true },
      { id: 'pair:1', source: '1', target: '0', data: { __pairing__: true }, selected: false },
    ];
    expect(reconcileCanvasEdges(previous, structuredClone(previous))).toBe(previous);
    const incoming = structuredClone(previous);
    incoming[0].label = 'no';
    incoming[0].selected = false;
    const next = reconcileCanvasEdges(previous, incoming);
    expect(next[0].label).toBe('no');
    expect(next[0].selected).toBe(true);
    expect(next[1]).toBe(previous[1]);
    expect(reconcileCanvasEdges(next, [incoming[1]])).toEqual([previous[1]]);
  });
});
