import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { CanvasNodeSearch } from '../CanvasNodeSearch';
import { useExecStreamStore } from '@/stores/exec-stream';
const graph = vi.hoisted(() => ({
  nodes: [
    {id: 'node_1', position: {x: 0, y: 0}, data: {node_name: 'Prepare', node_type: 'CodeNode'}},
    {id: 'node_2', position: {x: 1000, y: 0}, data: {node_name: 'Summarize', node_type: 'PromptNode'}},
  ],
  setNodes: vi.fn(), setEdges: vi.fn(), setViewport: vi.fn(), bounds: vi.fn(),
}));
vi.mock('@xyflow/react', () => ({
  useNodes: () => graph.nodes,
  useReactFlow: () => ({setNodes: graph.setNodes, setEdges: graph.setEdges, setViewport: graph.setViewport, getNode: (id: string) => graph.nodes.find(n => n.id === id)}),
  getNodesBounds: () => ({x: 1000, y: 0, width: 224, height: 500}),
  getViewportForBounds: graph.bounds,
}));
vi.mock('react-i18next', () => ({useTranslation: () => ({t: (key: string, fallback?: string) => fallback ?? key})}));
describe('find existing canvas nodes', () => {
  beforeEach(() => {
    graph.setNodes.mockClear(); graph.setViewport.mockClear(); graph.bounds.mockClear();
    useExecStreamStore.setState({perNode: {node_1: {status: 'error'}, node_2: {status: 'running'}}});
  });
  afterEach(() => {cleanup();vi.restoreAllMocks();});
  it('filters by execution status and matches node names and types', async () => {
    render(<CanvasNodeSearch />);
    fireEvent.click(screen.getByRole('button', {name: 'canvasNavigation.find'}));
    fireEvent.click(screen.getByRole('button', {name: 'canvasNavigation.error'}));
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('option')).toHaveTextContent('Prepare');
    fireEvent.click(screen.getByRole('button', {name: 'canvasNavigation.all'}));
    fireEvent.change(screen.getByRole('combobox'), {target: {value: 'PromptNode'}});
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(1));
    expect(screen.getByRole('option')).toHaveTextContent('Summarize');
  });
  it('centers the selected card above an overlapping mobile inspector', async () => {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      return (this.hasAttribute('data-workflow-inspector')
        ? {left: 0, right: 390, top: 400, width: 390, height: 444}
        : {left: 0, right: 390, top: 100, width: 390, height: 744}) as DOMRect;
    });
    render(<><div data-node-picker-surface><CanvasNodeSearch /></div><div data-workflow-inspector /></>);
    fireEvent.click(screen.getByRole('button', {name: 'canvasNavigation.find'}));
    fireEvent.click(screen.getByRole('option', {name: /Summarize/}));
    await waitFor(() => expect(graph.bounds).toHaveBeenCalledWith({x: 1000, y: 0, width: 224, height: 160}, 390, 300, 0.7, 1, 0.3));
    const selected = graph.setNodes.mock.calls[0][0](graph.nodes);
    expect(selected.map((n: {selected: boolean}) => n.selected)).toEqual([false, true]);
  });
});
