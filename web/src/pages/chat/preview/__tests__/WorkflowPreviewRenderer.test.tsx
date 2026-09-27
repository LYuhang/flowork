import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkflowPreviewRenderer } from '../WorkflowPreviewRenderer';
import { useWorkflowSnapshot } from '@/pages/canvas/WorkflowSnapshotContext';

const state = vi.hoisted(() => ({
  result: {} as Record<string, unknown>,
  options: {} as Record<string, unknown>,
  flow: {} as Record<string, unknown>,
}));
vi.mock('@tanstack/react-query', () => ({ useQuery: (options: Record<string, unknown>) => { state.options = options; return state.result; } }));
vi.mock('@/lib/api/queries/workflow', () => ({ workflowAtQuery: (id: string, v: number, sv: number) => ({ queryKey: ['workflow-at', id, v, sv] }) }));
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }: { children: React.ReactNode }) => children,
  useNodesInitialized: () => false,
  useReactFlow: () => ({ fitView: vi.fn() }),
  useStore: (selector: (state: { width: number; height: number }) => unknown) => selector({ width: 0, height: 0 }),
}));
vi.mock('@/pages/canvas/inspector/NodeTab', () => ({ NodeTab: ({ readOnly }: { readOnly: boolean }) => {
  const graph = useWorkflowSnapshot();
  return <div data-testid="shared-inspector">{String(readOnly)}:{JSON.stringify(graph)}</div>;
} }));
vi.mock('@/pages/canvas/WorkflowGraph', () => ({
  workflowDictToNodesEdges: (graph: Record<string, unknown>) => ({ nodes: Object.entries(graph).map(([id, data]) => ({ id, data, type: 'custom' })), edges: [] }),
  WorkflowGraph: (props: Record<string, unknown>) => {
    state.flow = props;
    return <button
      onClick={() => (props.onNodeClick as (event: unknown, node: { id: string }) => void)({}, { id: 'start' })}
      onDoubleClick={() => (props.onNodeDoubleClick as (event: unknown, node: { id: string }) => void)({}, { id: 'start' })}>Select start</button>;
  },
}));

describe('WorkflowPreviewRenderer', () => {
  beforeEach(() => {
    state.result = { isFetching: false, isPending: false, isError: false,
      data: { meta: { workflow_name: 'Flow' }, workflow: { start: { node_type: 'StartNode', node_config: { example: 'detail' } } } } };
    state.flow = {};
  });

  it('pins the query but links to the latest canvas without changing the global editor', () => {
    render(<WorkflowPreviewRenderer workflowId="wf" version="v2.sv3" />);
    expect(state.options.queryKey).toEqual(['workflow-preview', 'wf', 'v2.sv3']);
    expect(state.options.refetchOnMount).toBe('always');
    expect(state.flow.nodesDraggable).toBe(false);
    expect(state.flow.nodesConnectable).toBe(false);
    expect(screen.getByRole('link', { name: 'Open latest canvas' })).toHaveAttribute('href', '/workflow/wf');
    expect(screen.getByText(/v2.sv3/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Select start' }));
    expect(screen.queryByTestId('shared-inspector')).not.toBeInTheDocument();
    fireEvent.doubleClick(screen.getByRole('button', { name: 'Select start' }));
    expect(screen.getByRole('region', { name: 'Node details' })).toHaveTextContent('detail');
    expect(screen.getByTestId('shared-inspector')).toHaveTextContent('true:');
    expect(state.flow.showInteractiveControls).toBe(false);
    expect(state.flow.deleteKeyCode).toBeNull();
    expect(screen.getByRole('link', { name: 'Open in a new Preview tab' })).toHaveAttribute('href', '/preview?type=workflow&workflowId=wf&version=v2.sv3');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByTestId('shared-inspector')).not.toBeInTheDocument();
  });

  it('opens the shared read-only Inspector on the right with one click in a standalone page', () => {
    render(<WorkflowPreviewRenderer workflowId="wf" version="v2.sv3" allowOpenInNewPage={false} inspectorPlacement="right" />);
    fireEvent.click(screen.getByRole('button', { name: 'Select start' }));
    expect(screen.getByRole('region', { name: 'Node details' })).toHaveClass('md:border-l');
    expect(screen.getByTestId('shared-inspector')).toHaveTextContent('true:');
    expect(screen.queryByRole('link', { name: 'Open in a new Preview tab' })).not.toBeInTheDocument();
  });

  it('does not render cached private nodes while rechecking authorization', () => {
    state.result.isFetching = true;
    render(<WorkflowPreviewRenderer workflowId="wf" version="v2.sv3" />);
    expect(screen.queryByRole('button', { name: 'Select start' })).not.toBeInTheDocument();
  });

  it('hides cached data after revoked access and offers retry', () => {
    state.result.isError = true;
    state.result.refetch = vi.fn();
    render(<WorkflowPreviewRenderer workflowId="wf" version="v2.sv3" />);
    expect(screen.getByRole('alert')).toHaveTextContent('no longer have access');
    expect(screen.queryByRole('button', { name: 'Select start' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.result.refetch).toHaveBeenCalled();
  });
});
