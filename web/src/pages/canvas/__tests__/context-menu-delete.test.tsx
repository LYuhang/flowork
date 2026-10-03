/**
 * Stream 1 — ContextMenuLayer delete-node / delete-edge wiring.
 *
 * The menu items call the edit-store actions (`removeNode` /
 * `disconnectNodes`) rather than xyflow's `deleteElements`, so the deletion
 * persists in the draft (the source of truth). We override only
 * `useNodes`/`useEdges` (to seed a selection) while keeping the rest of
 * `@xyflow/react` real because the context-menu actions use genuine provider
 * state from the surrounding `ReactFlowProvider`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CanvasChatContext } from '../CanvasChatContext';
import { ReactFlowProvider } from '@xyflow/react';

const selection = {
  nodes: [] as { id: string; selected: boolean }[],
  edges: [] as { id: string; source: string; target: string; selected: boolean }[],
};

vi.mock('@xyflow/react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@xyflow/react')>();
  return {
    ...actual,
    useNodes: () => selection.nodes,
    useEdges: () => selection.edges,
  };
});

import { ContextMenuLayer } from '@/pages/canvas/ContextMenuLayer';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

function renderLayer() {
  return render(
    <ReactFlowProvider>
      <ContextMenuLayer>
        <div data-testid="pane" style={{ width: 200, height: 200 }} />
      </ContextMenuLayer>
    </ReactFlowProvider>,
  );
}

function openMenu() {
  // Radix ContextMenu opens on a `contextmenu` event on its trigger.
  fireEvent.contextMenu(screen.getByTestId('pane'));
}

beforeEach(() => {
  selection.nodes = [];
  selection.edges = [];
  useWorkflowEditStore.getState().setDraft(null);
});

describe('ContextMenuLayer delete-node', () => {
  it('calls removeNode for the selected node (persists in the draft)', () => {
    selection.nodes = [{ id: 'node_2', selected: true }];
    useWorkflowEditStore.getState().setDraft({
      node_1: { node_id: 'node_1', node_type: 'StartNode', children: ['node_2'] },
      node_2: { node_id: 'node_2', node_type: 'EndNode', children: [] },
    });

    renderLayer();
    openMenu();

    const item = screen.getByText('Delete node');
    fireEvent.click(item);

    const draft = useWorkflowEditStore.getState().draft as Record<string, any>;
    expect(draft.node_2).toBeUndefined();
    expect(draft.node_1.children).toEqual([]);
  });
});

describe('ContextMenuLayer delete-edge', () => {
  it('calls disconnectNodes for the selected edge (persists in the draft)', () => {
    selection.edges = [
      { id: 'node_1->node_2', source: 'node_1', target: 'node_2', selected: true },
    ];
    useWorkflowEditStore.getState().setDraft({
      node_1: { node_id: 'node_1', node_type: 'StartNode', children: ['node_2'] },
      node_2: { node_id: 'node_2', node_type: 'EndNode', children: [] },
    });

    renderLayer();
    openMenu();

    fireEvent.click(screen.getByText('Delete selected edge'));

    const draft = useWorkflowEditStore.getState().draft as Record<string, any>;
    expect(draft.node_1.children).toEqual([]);
  });
});

describe('ContextMenuLayer contextual conversation', () => {
  it('uses the right-clicked node even when a different node is selected', async () => {
    const open = vi.fn();
    selection.nodes = [{ id: 'selected', selected: true }, { id: 'clicked', selected: false }];
    render(<ReactFlowProvider><CanvasChatContext.Provider value={open}>
      <ContextMenuLayer><div className="react-flow__node" data-id="clicked" data-testid="clicked-node" /></ContextMenuLayer>
    </CanvasChatContext.Provider></ReactFlowProvider>);
    fireEvent.contextMenu(screen.getByTestId('clicked-node'), { clientX: 120, clientY: 160 });
    fireEvent.click(screen.getByText('Chat here'));
    await waitFor(() => expect(open).toHaveBeenCalledWith({ kind: 'node', node_id: 'clicked' }, { x: 120, y: 160 }));
  });

  it('binds the actual edge endpoints instead of a selected edge', async () => {
    const open = vi.fn();
    selection.edges = [{ id: 'from->to', source: 'from', target: 'to', selected: false }];
    render(<ReactFlowProvider><CanvasChatContext.Provider value={open}>
      <ContextMenuLayer><svg><g className="react-flow__edge" data-id="from->to" data-testid="clicked-edge" /></svg></ContextMenuLayer>
    </CanvasChatContext.Provider></ReactFlowProvider>);
    fireEvent.contextMenu(screen.getByTestId('clicked-edge'), { clientX: 120, clientY: 160 });
    fireEvent.click(screen.getByText('Chat here'));
    await waitFor(() => expect(open).toHaveBeenCalledWith(expect.objectContaining({ kind: 'edge', source: 'from', target: 'to' }), { x: 120, y: 160 }));
  });

  it('does not treat a loop pairing indicator as an ordinary edge', () => {
    const open = vi.fn();
    selection.edges = [{ id: 'pair:begin->end', source: 'begin', target: 'end', selected: false }];
    render(<ReactFlowProvider><CanvasChatContext.Provider value={open}>
      <ContextMenuLayer><svg><g className="react-flow__edge" data-id="pair:begin->end" data-testid="pair-edge" /></svg></ContextMenuLayer>
    </CanvasChatContext.Provider></ReactFlowProvider>);
    fireEvent.contextMenu(screen.getByTestId('pair-edge'));
    expect(screen.getByText('Chat here').closest('[role="menuitem"]')).toHaveAttribute('data-disabled');
    expect(open).not.toHaveBeenCalled();
  });
});
