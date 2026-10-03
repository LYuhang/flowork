/**
 * Stream 8 — Canvas feedback wiring:
 *   - `workflowDictToNodesEdges` injects each node's local `nodeWarnings` into
 *     `data.__warnings__` (the seam the CustomNode badge reads).
 *   - It emits an ADDITIONAL dashed/labeled pairing edge for a paired
 *     Parallel/Loop START, marked non-child (`data.__pairing__`),
 *     non-selectable + non-deletable.
 *   - The `<Canvas>` host renders a centered empty-state overlay at 0 nodes.
 *
 * The pure-transform assertions follow the existing `canvas-connect-edges`
 * pattern (exercise the same seam the component uses without fighting a mocked
 * ReactFlow). The overlay test renders the host with a stubbed ReactFlow so we
 * assert OUR overlay markup, not xyflow internals.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import en from '@/lib/i18n/locales/en.json';

// Stub ReactFlow + hooks so the host renders without a real flow runtime.
vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ children }: { children?: React.ReactNode }) => (
    <div data-testid="rf">{children}</div>
  ),
  Background: () => null,
  Controls: () => null,
  MiniMap: () => null,
  // Render data-handle so this mock is byte-identical to the one in
  // custom-node-feedback.test.tsx — under vitest `isolate:false` sibling files
  // share the module graph, so a DIVERGENT @xyflow/react mock would clobber the
  // other file's expectations (see feedback_vitest_isolate_false). Identical
  // factories are collision-safe regardless of evaluation order.
  Handle: ({
    type,
    className,
    id,
    isConnectable,
  }: {
    type: string;
    className?: string;
    id?: string;
    isConnectable?: boolean;
  }) => (
    <div
      data-handle={type}
      data-handle-id={id}
      data-connectable={isConnectable === false ? 'false' : undefined}
      className={className}
    />
  ),
  Position: { Left: 'left', Right: 'right', Bottom: 'bottom' },
  useNodesState: (init: unknown[]) => [init, vi.fn(), vi.fn()],
  useEdgesState: (init: unknown[]) => [init, vi.fn(), vi.fn()],
  useReactFlow: () => ({ screenToFlowPosition: (p: unknown) => p }),
}));

import { Canvas, workflowDictToNodesEdges } from '@/pages/canvas/Canvas';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useUIStore } from '@/stores/ui';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

function withI18n(node: React.ReactNode) {
  return render(<I18nextProvider i18n={testI18n}>{node}</I18nextProvider>);
}

describe('workflowDictToNodesEdges — warnings injection', () => {
  it('attaches __warnings__ to a node that has local warnings', () => {
    const { nodes } = workflowDictToNodesEdges({
      node_1: {
        node_name: 'lb',
        node_type: 'LoopBeginNode',
        children: [],
        node_config: { loop_end_node_id: null },
      },
    });
    const n1 = nodes.find((n) => n.id === 'node_1')!;
    expect(n1.data.__warnings__).toEqual(['canvas.warn.unpairedLoop']);
  });

  it('attaches an empty __warnings__ to a clean node', () => {
    const { nodes } = workflowDictToNodesEdges({
      node_1: { node_name: 'start', node_type: 'StartNode', children: [] },
    });
    expect(nodes[0].data.__warnings__).toEqual([]);
  });
});

describe('workflowDictToNodesEdges — pairing edges', () => {
  it('emits NO pairing edge for a paired ParallelStart (parallel has no jump-back)', () => {
    // Only Loop draws a pairing edge — the Parallel partner is reached through
    // the ordinary `children` graph, so a pairing edge there is visual noise and
    // is intentionally NOT emitted (see `pairingEdgeFor`).
    const { edges } = workflowDictToNodesEdges({
      node_1: {
        node_name: 'p',
        node_type: 'ParallelStartNode',
        children: [],
        node_config: { branches: {}, parallel_end_node_id: 'node_2' },
      },
      node_2: {
        node_name: 'pe',
        node_type: 'ParallelEndNode',
        children: [],
        node_config: { parallel_start_node_id: 'node_1' },
      },
    });
    expect(edges.find((e) => e.id.startsWith('pair:'))).toBeUndefined();
  });

  it('emits a "loop" pairing edge for a paired LoopBegin running end-bottom → begin-bottom', () => {
    const { edges } = workflowDictToNodesEdges({
      node_1: {
        node_name: 'lb',
        node_type: 'LoopBeginNode',
        children: [],
        node_config: { loop_end_node_id: 'node_2' },
      },
      node_2: {
        node_name: 'le',
        node_type: 'LoopEndNode',
        children: [],
        node_config: { loop_begin_node_id: 'node_1' },
      },
    });
    const pair = edges.find((e) => e.id === 'pair:node_1->node_2')!;
    expect(pair.label).toBe('loop');
    // Runs from the LoopEnd (node_2) BOTTOM → LoopBegin (node_1) BOTTOM via the
    // dedicated non-connectable handles, routed as a smoothstep that dips below.
    expect(pair.source).toBe('node_2');
    expect(pair.target).toBe('node_1');
    expect(pair.sourceHandle).toBe('loop-back-source');
    expect(pair.targetHandle).toBe('loop-back-target');
    expect(pair.type).toBe('smoothstep');
    expect(pair.deletable).toBe(false);
    expect(pair.selectable).toBe(false);
    expect((pair.data as { __pairing__?: boolean }).__pairing__).toBe(true);
  });

  it('emits NO pairing edge when the pointer is unset', () => {
    const { edges } = workflowDictToNodesEdges({
      node_1: {
        node_name: 'p',
        node_type: 'ParallelStartNode',
        children: [],
        node_config: { branches: {}, parallel_end_node_id: null },
      },
    });
    expect(edges.find((e) => e.id.startsWith('pair:'))).toBeUndefined();
  });

  it('does not confuse a pairing edge with a child edge (distinct id namespace)', () => {
    const { edges } = workflowDictToNodesEdges({
      node_1: {
        node_name: 'lb',
        node_type: 'LoopBeginNode',
        children: ['node_3'], // a real child body-head edge
        node_config: { loop_end_node_id: 'node_2' },
      },
      node_2: { node_name: 'le', node_type: 'LoopEndNode', children: [] },
      node_3: { node_name: 'body', node_type: 'CodeNode', children: [] },
    });
    const child = edges.find((e) => e.id === 'node_1->node_3')!;
    const pair = edges.find((e) => e.id === 'pair:node_1->node_2')!;
    expect((child.data as { __pairing__?: boolean } | undefined)?.__pairing__).toBeUndefined();
    expect((pair.data as { __pairing__?: boolean }).__pairing__).toBe(true);
  });
});

describe('Canvas — empty-state overlay', () => {
  it('renders the hint overlay at 0 nodes', () => {
    useWorkflowEditStore.getState().setDraft({});
    withI18n(<Canvas />);
    const overlay = document.querySelector('[data-canvas-empty-state]');
    expect(overlay).not.toBeNull();
    expect(screen.getByText(en['nodePicker.emptyHint'])).toBeInTheDocument();
  });

  it('does NOT render the overlay when nodes exist', () => {
    useWorkflowEditStore.getState().setDraft({
      node_1: { node_name: 'start', node_type: 'StartNode', children: [] },
    });
    withI18n(<Canvas />);
    expect(document.querySelector('[data-canvas-empty-state]')).toBeNull();
  });
});

// UX-15: double-clicking the BLANK canvas pane toggles the Inspector — and in
// particular CLOSES it when it's already open (reclaim canvas width). A
// double-click on a NODE must NOT close it (that path opens the node scope).
describe('Canvas — double-click pane toggles the Inspector (UX-15)', () => {
  it('closes the Inspector when the blank pane is double-clicked while open', () => {
    useWorkflowEditStore.getState().setDraft({});
    useUIStore.getState().setInspectorOpen(true);
    withI18n(<Canvas />);
    const pane = document.querySelector('[data-canvas-pane]')!;
    fireEvent.doubleClick(pane);
    expect(useUIStore.getState().inspectorOpen).toBe(false);
  });

  it('opens the Inspector when the blank pane is double-clicked while closed', () => {
    useWorkflowEditStore.getState().setDraft({});
    useUIStore.getState().setInspectorOpen(false);
    withI18n(<Canvas />);
    const pane = document.querySelector('[data-canvas-pane]')!;
    fireEvent.doubleClick(pane);
    expect(useUIStore.getState().inspectorOpen).toBe(true);
  });

  it('does NOT close the Inspector when a NODE is double-clicked', () => {
    useWorkflowEditStore.getState().setDraft({});
    useUIStore.getState().setInspectorOpen(true);
    withI18n(<Canvas />);
    const pane = document.querySelector('[data-canvas-pane]')!;
    // Simulate the double-click landing on a node: the wrapper handler walks up
    // from the target via `.closest('.react-flow__node, .react-flow__edge')`
    // and must ignore it (leaving the sider open).
    const nodeEl = document.createElement('div');
    nodeEl.className = 'react-flow__node';
    pane.appendChild(nodeEl);
    fireEvent.doubleClick(nodeEl);
    expect(useUIStore.getState().inspectorOpen).toBe(true);
  });
});
