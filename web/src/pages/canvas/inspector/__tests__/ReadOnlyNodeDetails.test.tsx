import userEvent from '@testing-library/user-event';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ReadOnlyNodeDetails } from '../ReadOnlyNodeDetails';

vi.mock('../NodeTab', () => ({ NodeTab: (props: Record<string, unknown>) => <div data-testid="node-properties">{JSON.stringify(props)}</div> }));
vi.mock('../../nodes/NodeJsonPreview', () => ({ NodeJsonPreview: ({ value }: { value: unknown }) => <pre>{JSON.stringify(value)}</pre> }));

describe('read-only node details', () => {
  it('uses explicit snapshot selection and shows only the selected node execution', async () => {
    const events = [
      { seq: 1, type: 'node_event', node_id: 'a', status: 'running', inputs: { input: 'chosen-input' } },
      { seq: 2, type: 'node_event', node_id: 'b', status: 'success', output: 'other-node-output' },
      { seq: 3, type: 'node_event', node_id: 'a', status: 'error', output: null, error_message: 'chosen-error' },
    ];
    const { rerender } = render(<ReadOnlyNodeDetails workflowId="wf" nodeId="a" events={events} executionStatus="failed" />);
    expect(screen.getByTestId('node-properties')).toHaveTextContent('"readOnly":true');
    expect(screen.getByTestId('node-properties')).toHaveTextContent('"selectedNodeId":"a"');
    expect(screen.queryByText(/chosen-input/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', {name:'Run info'}));
    expect(screen.queryByTestId('node-properties')).not.toBeInTheDocument();
    expect(screen.getByText(/chosen-input/)).toBeInTheDocument();
    expect(screen.getByText('{"output":null}')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('chosen-error');
    expect(screen.queryByText(/other-node-output/)).not.toBeInTheDocument();
    rerender(<ReadOnlyNodeDetails workflowId="wf" nodeId="b" events={events} executionStatus="failed" />);
    expect(screen.getByText(/other-node-output/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    rerender(<ReadOnlyNodeDetails workflowId="wf" nodeId="b" events={[]} executionStatus="running" />);
    expect(screen.getByText('This node has no execution record in this run.')).toBeInTheDocument();
    expect(screen.queryByText(/other-node-output/)).not.toBeInTheDocument();
  });

  it('does not imply a saved version has an execution result', async () => {
    render(<ReadOnlyNodeDetails workflowId="wf" nodeId="a" />);
    expect(screen.getByTestId('node-properties')).toBeInTheDocument();
    expect(screen.queryByText('This node has no execution record in this run.')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', {name:'Run info'}));
    expect(screen.getByText('This is a workflow version snapshot with no attached execution.')).toBeInTheDocument();
  });
});
