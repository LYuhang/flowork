import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Position, type EdgeProps } from '@xyflow/react';
import { RoutedEdge } from '../RoutedEdge';

vi.mock('@xyflow/react', async (original) => ({
  ...await original<typeof import('@xyflow/react')>(),
  BaseEdge: ({ path, label }: { path: string; label: string }) => <div data-testid="routed" data-path={path}>{label}</div>,
  BezierEdge: () => <div data-testid="ordinary" />,
  SmoothStepEdge: () => <div data-testid="loop" />,
}));
const props: EdgeProps = {
  id: 'a->b', source: 'a', target: 'b', sourceX: 224, sourceY: 70, targetX: 300, targetY: 70,
  sourcePosition: Position.Right, targetPosition: Position.Left,
  data: { layoutPoints: [{ x: 224, y: 70 }, { x: 260, y: 70 }, { x: 260, y: 200 }, { x: 300, y: 200 }, { x: 300, y: 70 }] },
  label: 'Approved',
};
describe('compact routed edges', () => {
  it('renders the supplied bends and branch label', () => {
    render(<RoutedEdge {...props} />);
    expect(screen.getByTestId('routed')).toHaveAttribute('data-path', 'M 224 70 L 260 70 L 260 200 L 300 200 L 300 70');
    expect(screen.getByText('Approved')).toBeInTheDocument();
  });
  it('uses ordinary routing when a handle moves inside an unchanged node box', () => {
    render(<RoutedEdge {...props} sourceY={90} />);
    expect(screen.getByTestId('ordinary')).toBeInTheDocument();
  });
  it('preserves loop routing when a saved loop handle moves', () => {
    render(<RoutedEdge {...props} targetY={90} data={{ ...props.data, __pairing__: true }} />);
    expect(screen.getByTestId('loop')).toBeInTheDocument();
  });
});
