/**
 * Stream 1 — Canvas graph-edit wiring, exercised through the SAME seam the
 * component uses: the edit-store mutation + the pure
 * `workflowDictToNodesEdges` re-derive. The component's `onConnect` /
 * edge-`remove` handlers do nothing but call `connectNodes` /
 * `disconnectNodes`, then xyflow re-derives from the draft — so asserting
 * "after disconnect, the transform emits no edge for that pair" proves the
 * persist-and-redraw contract without fighting a mocked ReactFlow render.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { workflowDictToNodesEdges } from '@/pages/canvas/Canvas';
import { saveCompactLayout } from '@/pages/canvas/layout-routes';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

function api() {
  return useWorkflowEditStore.getState();
}

beforeEach(() => {
  api().setDraft(null);
});

describe('connect → edge re-derives from children', () => {
  it('onConnect (connectNodes) adds the edge to the transform output', () => {
    api().setDraft({
      node_1: { node_id: 'node_1', node_type: 'StartNode', children: [] },
      node_2: { node_id: 'node_2', node_type: 'EndNode', children: [] },
    });
    expect(workflowDictToNodesEdges(api().draft).edges).toHaveLength(0);

    api().connectNodes('node_1', 'node_2');
    const { edges } = workflowDictToNodesEdges(api().draft);
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: 'node_1', target: 'node_2', id: 'node_1->node_2' });
  });
});

describe('delete edge persists → re-sync emits no edge', () => {
  it('disconnectNodes removes the target from children so the transform emits no edge', () => {
    api().setDraft({
      node_1: { node_id: 'node_1', node_type: 'StartNode', children: ['node_2'] },
      node_2: { node_id: 'node_2', node_type: 'EndNode', children: [] },
    });
    expect(workflowDictToNodesEdges(api().draft).edges).toHaveLength(1);

    api().disconnectNodes('node_1', 'node_2');
    expect(api().draft!.node_1).toMatchObject({ children: [] });
    expect(workflowDictToNodesEdges(api().draft).edges).toHaveLength(0);
  });
});

describe('ConditionNode connect/disconnect — others=default, cards for extras', () => {
  /** Read node_1's conditions list from the live draft. */
  function conds() {
    return (api().draft!.node_1 as any).node_config.conditions as any[];
  }
  function others() {
    return conds().find((c) => c.condition_str?.trim() === 'others');
  }
  function nonOthers() {
    return conds().filter((c) => c.condition_str?.trim() !== 'others');
  }

  beforeEach(() => {
    api().setDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        children: [],
        node_config: { conditions: [] },
      },
      node_a: { node_id: 'node_a', node_type: 'CodeNode', children: [] },
      node_b: { node_id: 'node_b', node_type: 'CodeNode', children: [] },
    });
  });

  it('0 children → [others→null]; the FIRST connect makes others the default target', () => {
    api().connectNodes('node_1', 'node_a');
    // [others→A]: exactly one card, the others card, targeting A.
    expect(conds()).toHaveLength(1);
    expect(nonOthers()).toHaveLength(0);
    expect(others().next_node_id).toBe('node_a');
    expect((api().draft!.node_1 as any).children).toEqual(['node_a']);
  });

  it('second connect appends a branch card BEFORE others (others stays last)', () => {
    api().connectNodes('node_1', 'node_a'); // [others→A]
    api().connectNodes('node_1', 'node_b'); // [branch→B, others→A]
    const list = conds();
    expect(list).toHaveLength(2);
    // others is LAST.
    expect(list[list.length - 1].condition_str?.trim()).toBe('others');
    expect(others().next_node_id).toBe('node_a');
    // the new branch card targets B, condition_str empty.
    const branch = nonOthers()[0];
    expect(branch.next_node_id).toBe('node_b');
    expect(branch.condition_str).toBe('');
    expect((api().draft!.node_1 as any).children).toEqual(['node_a', 'node_b']);
  });

  it('connect is idempotent — re-connecting an already-mapped target is a no-op', () => {
    api().connectNodes('node_1', 'node_a');
    api().connectNodes('node_1', 'node_a');
    expect(conds()).toHaveLength(1);
    expect(others().next_node_id).toBe('node_a');
  });

  it('disconnecting a non-others branch DELETES its card', () => {
    api().connectNodes('node_1', 'node_a'); // [others→A]
    api().connectNodes('node_1', 'node_b'); // [branch→B, others→A]
    api().disconnectNodes('node_1', 'node_b'); // branch card gone → [others→A]
    expect(conds()).toHaveLength(1);
    expect(nonOthers()).toHaveLength(0);
    expect(others().next_node_id).toBe('node_a');
    expect((api().draft!.node_1 as any).children).toEqual(['node_a']);
  });

  it("disconnecting others' target NULLs others (keeps the card)", () => {
    api().connectNodes('node_1', 'node_a'); // [others→A]
    api().disconnectNodes('node_1', 'node_a'); // [others→null]
    expect(conds()).toHaveLength(1);
    expect(others().next_node_id).toBeNull();
    expect((api().draft!.node_1 as any).children).toEqual([]);
  });
});

describe('edge labels for Condition / ParallelStart sources', () => {
  it('labels each Condition out-edge with its condition_name (matched by next_node_id)', () => {
    api().setDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        children: ['node_2', 'node_3'],
        node_config: {
          conditions: [
            { condition_name: 'high', condition_str: '{x}>1', next_node_id: 'node_2' },
            { condition_name: 'others', condition_str: 'others', next_node_id: 'node_3' },
          ],
        },
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', children: [] },
      node_3: { node_id: 'node_3', node_type: 'CodeNode', children: [] },
    });
    const { edges } = workflowDictToNodesEdges(api().draft);
    const e2 = edges.find((e) => e.target === 'node_2');
    const e3 = edges.find((e) => e.target === 'node_3');
    expect(e2?.label).toBe('high');
    expect(e3?.label).toBe('others');
  });

  it('labels ParallelStart out-edges with the branch key', () => {
    api().setDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ParallelStartNode',
        children: ['node_2'],
        node_config: {
          branches: { sentiment: { branch_description: '', next_node_id: 'node_2' } },
          parallel_end_node_id: null,
        },
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', children: [] },
    });
    const { edges } = workflowDictToNodesEdges(api().draft);
    expect(edges.find((e) => e.target === 'node_2')?.label).toBe('sentiment');
  });

  it('emits no label for a plain source', () => {
    api().setDraft({
      node_1: { node_id: 'node_1', node_type: 'StartNode', children: ['node_2'] },
      node_2: { node_id: 'node_2', node_type: 'EndNode', children: [] },
    });
    expect(workflowDictToNodesEdges(api().draft).edges[0].label).toBeUndefined();
  });
});


it('re-projects saved routes and drops them after changing the graph', () => {
  api().setDraft({ node_1: { node_type: 'StartNode', children: ['node_2'] }, node_2: { node_type: 'EndNode', children: [] } });
  const graph = workflowDictToNodesEdges(api().draft);
  api().applyEdit(wf => saveCompactLayout(wf, graph.nodes, graph.edges, {
    width: 524, height: 140,
    positions: { node_1: { x: 0, y: 0 }, node_2: { x: 300, y: 0 } },
    routes: { 'node_1->node_2': [{ x: 224, y: 70 }, { x: 300, y: 70 }] },
  }));
  expect(workflowDictToNodesEdges(api().draft).edges[0]).toMatchObject({ type: 'routed', data: { layoutPoints: [{ x: 224, y: 70 }, { x: 300, y: 70 }] } });
  api().disconnectNodes('node_1', 'node_2');
  expect(workflowDictToNodesEdges(api().draft).edges).toHaveLength(0);
  api().undo();
  expect(workflowDictToNodesEdges(api().draft).edges[0].type).toBe('routed');
});
