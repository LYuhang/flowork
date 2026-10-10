import { describe, expect, it } from 'vitest';
import { layoutGeometryMatches, readLayoutRoutes, routedPath, saveCompactLayout } from '../layout-routes';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { parseUploadedWorkflow, serializeWorkflow } from '@/lib/workflow/io';
import type { Edge, Node } from '@xyflow/react';

const nodes: Node[] = ['a', 'b'].map((id, i) => ({ id, data: {}, position: { x: i * 300, y: 20 }, measured: { width: 224, height: 140 } }));
const edges: Edge[] = [{ id: 'a->b', source: 'a', target: 'b' }];
function saved() {
  const wf = { a: { __attributes__: { color: 'blue' } }, b: {}, __meta__: { name: 'Keep name' } };
  return saveCompactLayout(wf, nodes, edges, { width: 524, height: 140, positions: { a: nodes[0].position, b: nodes[1].position }, routes: { 'a->b': [{ x: 224, y: 70 }, { x: 300, y: 70 }] } });
}

describe('saved compact layout paths', () => {
  it('exports and imports routes, and restores them with the same undo step as positions', () => {
    const draft = { node_1: { node_type: 'StartNode', children: ['node_2'] }, node_2: { node_type: 'EndNode', children: [] } };
    const measured = nodes.map((n, i) => ({ ...n, id: `node_${i + 1}` }));
    const links = [{ id: 'node_1->node_2', source: 'node_1', target: 'node_2' }];
    const store = useWorkflowEditStore;
    store.getState().setDraft(draft);
    try {
      store.getState().applyEdit(wf => saveCompactLayout(wf, measured, links, {
        width: 524, height: 140, positions: { node_1: measured[0].position, node_2: measured[1].position },
        routes: { 'node_1->node_2': [{ x: 224, y: 70 }, { x: 300, y: 70 }] },
      }));
      const result = store.getState().draft!;
      const imported = parseUploadedWorkflow(JSON.parse(serializeWorkflow(result))).workflow;
      expect(readLayoutRoutes(imported, measured, links)).not.toBeNull();
      expect(store.getState().undoStack).toHaveLength(1);
      store.getState().undo();
      expect(store.getState().draft).toEqual(draft);
      store.getState().redo();
      expect(store.getState().draft).toEqual(result);
    } finally { store.getState().setDraft(null); }
  });
  it('survives JSON roundtrip and preserves unrelated metadata', () => {
    const wf = JSON.parse(JSON.stringify(saved()));
    expect(wf.__meta__.name).toBe('Keep name');
    expect(wf.a.__attributes__.color).toBe('blue');
    expect(readLayoutRoutes(wf, nodes, edges)?.edges[0].points).toHaveLength(2);
  });
  it('invalidates all routes when a node moves, an edge changes, or a node is added', () => {
    const wf = saved();
    expect(readLayoutRoutes(wf, nodes.map(n => ({ ...n, position: { ...n.position, y: 21 } })), edges)).toBeNull();
    expect(readLayoutRoutes(wf, nodes, [{ ...edges[0], targetHandle: 'other' }])).toBeNull();
    expect(readLayoutRoutes(wf, [...nodes, { ...nodes[0], id: 'c' }], edges)).toBeNull();
  });
  it('requires actual measured geometry before rendering persisted routes', () => {
    const geometry = readLayoutRoutes(saved(), nodes, edges)!.nodes;
    expect(layoutGeometryMatches(geometry, nodes, true)).toBe(true);
    expect(layoutGeometryMatches(geometry, nodes.map(n => ({ ...n, measured: { width: 224, height: 300 } })), true)).toBe(false);
    expect(layoutGeometryMatches(geometry, nodes.map(n => ({ ...n, measured: undefined })), true)).toBe(false);
  });
  it('ignores malformed imported metadata', () => {
    for (const value of [null, {}, { version: 1, nodes: [null], edges: [] }, { version: 1, nodes: [], edges: [null] }]) {
      expect(readLayoutRoutes({ __meta__: { canvas_layout: value } }, nodes, edges)).toBeNull();
    }
  });
  it('places a label at the longest segment midpoint', () => {
    expect(routedPath([{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 100 }])).toEqual({ path: 'M 0 0 L 10 0 L 10 100', labelX: 10, labelY: 50 });
  });
});
