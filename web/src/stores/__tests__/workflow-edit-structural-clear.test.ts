/**
 * A structural edit clears stale execution results; a pure position drag
 * does NOT. Both go through the same `applyEdit` seam, so the discriminator is
 * `isStructuralDiff` (strips node `__attributes__` before comparing).
 */
import { beforeEach, describe, expect, it } from 'vitest';
import {
  useWorkflowEditStore,
  isStructuralDiff,
} from '@/stores/workflow-edit';
import { useExecStreamStore } from '@/stores/exec-stream';

const NODE: Record<string, unknown> = {
  node_1: {
    node_id: 'node_1',
    node_type: 'StartNode',
    node_name: 'start',
    children: ['node_2'] as string[],
    __attributes__: { x: 0, y: 0 },
  },
  node_2: {
    node_id: 'node_2',
    node_type: 'EndNode',
    node_name: 'end',
    children: [] as string[],
    __attributes__: { x: 100, y: 0 },
  },
  __meta__: {},
};

function seedExecResults() {
  // Simulate a finished run leaving per-node rings + a terminal status.
  useExecStreamStore.setState({
    wfId: 'wf_1',
    status: 'completed',
    perNode: { node_2: { status: 'completed', result: 'ok' } },
  });
}

const asRec = (v: unknown) => v as Record<string, unknown>;

describe('isStructuralDiff', () => {
  it('false when only node __attributes__ (position) differ', () => {
    const a = structuredClone(NODE);
    const b = structuredClone(NODE);
    asRec(asRec(b.node_1).__attributes__).x = 999;
    expect(isStructuralDiff(a, b)).toBe(false);
  });

  it('true on a connect/children change', () => {
    const a = structuredClone(NODE);
    const b = structuredClone(NODE);
    asRec(b.node_2).children = ['node_1'];
    expect(isStructuralDiff(a, b)).toBe(true);
  });

  it('true on a config / field change', () => {
    const a = structuredClone(NODE);
    const b = structuredClone(NODE);
    asRec(b.node_1).node_name = 'renamed';
    expect(isStructuralDiff(a, b)).toBe(true);
  });
});

describe('applyEdit clear-on-structural-edit', () => {
  beforeEach(() => {
    useWorkflowEditStore.getState().setDraft(structuredClone(NODE));
    useExecStreamStore.getState().reset();
  });

  it('a STRUCTURAL edit (connect) resets perNode + status', () => {
    seedExecResults();
    useWorkflowEditStore.getState().connectNodes('node_2', 'node_1');
    const exec = useExecStreamStore.getState();
    expect(exec.perNode).toEqual({});
    expect(exec.status).toBe('idle');
  });

  it('a STRUCTURAL edit (removeNode) resets perNode', () => {
    seedExecResults();
    useWorkflowEditStore.getState().removeNode('node_2');
    expect(useExecStreamStore.getState().perNode).toEqual({});
  });

  it('a STRUCTURAL field edit through raw applyEdit resets perNode', () => {
    seedExecResults();
    useWorkflowEditStore.getState().applyEdit((wf) => {
      (wf.node_1 as Record<string, unknown>).node_description = 'changed';
      return wf;
    });
    expect(useExecStreamStore.getState().perNode).toEqual({});
  });

  it('layout metadata keeps execution results, but execution settings still invalidate them', () => {
    seedExecResults();
    useWorkflowEditStore.getState().applyEdit(wf => ({ ...wf, __meta__: { canvas_layout: { version: 1, nodes: [], edges: [] } } }));
    expect(useExecStreamStore.getState().status).toBe('completed');
    useWorkflowEditStore.getState().applyEdit(wf => ({ ...wf, __meta__: { settings: { timeout: 60 } } }));
    expect(useExecStreamStore.getState().status).toBe('idle');
  });

  it('a POSITION drag does NOT clear exec results', () => {
    seedExecResults();
    // The Canvas position-lift mutates only __attributes__ on the dragged node.
    useWorkflowEditStore.getState().applyEdit((wf) => {
      const n = wf.node_1 as Record<string, unknown>;
      n.__attributes__ = { x: 500, y: 500 };
      return wf;
    });
    const exec = useExecStreamStore.getState();
    expect(exec.perNode).toEqual({ node_2: { status: 'completed', result: 'ok' } });
    expect(exec.status).toBe('completed');
  });
});
