import { describe, it, expect, beforeEach } from 'vitest';
import { readViewport } from '../canvas-viewport-storage';
describe('canvas viewport preferences', () => {
  beforeEach(() => localStorage.clear());
  it('restores valid pan and zoom without storing workflow data', () => {
    localStorage.setItem('view', JSON.stringify({ x: -1200, y: 32, zoom: 0.75 }));
    expect(readViewport('view')).toEqual({ x: -1200, y: 32, zoom: 0.75 });
  });
  it('ignores corrupt and out-of-range preferences', () => {
    for (const value of ['bad', 'null', '{}', '{"x":0,"y":0,"zoom":0}', '{"x":"0","y":0,"zoom":1}']) {
      localStorage.setItem('view', value);
      expect(readViewport('view')).toBeNull();
    }
  });
});

import { render, cleanup } from '@testing-library/react';
import { vi, afterEach } from 'vitest';
import { CanvasReadingView } from '../CanvasReadingView';
const graph = vi.hoisted(() => ({
  nodes: [{ id: 'start', position: { x: 0, y: 0 }, measured: { width: 224, height: 140 }, data: { node_type: 'StartNode' }, selected: true }],
  view: { x: 0, y: 0, zoom: 1 },
  overview: { x: 0, y: 0, zoom: 0.1 },
  setViewport: vi.fn(),
}));
vi.mock('@xyflow/react', () => ({
  useNodesInitialized: () => true,
  useNodes: () => graph.nodes,
  useStore: (selector: (s: {width: number; height: number}) => number) => selector({width: 1000, height: 800}),
  useViewport: () => graph.view,
  useReactFlow: () => ({ getViewport: () => graph.view, setViewport: graph.setViewport }),
  getNodesBounds: () => ({}),
  getViewportForBounds: () => graph.overview,
}));
describe('reading viewport behavior', () => {
  beforeEach(() => { localStorage.clear(); graph.setViewport.mockClear(); graph.view = { x: 0, y: 0, zoom: 1 }; graph.overview.zoom = 0.1; graph.nodes[0].position = {x: 0, y: 0}; });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });
  it('opens a long graph at a readable scale, restores a saved view, and fits short graphs', () => {
    const first = render(<CanvasReadingView storageKey="long" />);
    expect(graph.setViewport).toHaveBeenLastCalledWith(expect.objectContaining({zoom: 0.85}));
    first.unmount();
    localStorage.setItem('saved', JSON.stringify({x: -2000, y: 20, zoom: 0.7}));
    const second = render(<CanvasReadingView storageKey="saved" />);
    expect(graph.setViewport).toHaveBeenLastCalledWith({x: -2000, y: 20, zoom: 0.7});
    second.unmount();graph.overview.zoom = 0.9;
    render(<CanvasReadingView storageKey="short" />);
    expect(graph.setViewport).toHaveBeenLastCalledWith(graph.overview);
  });
  it('opens the start card above a mobile inspector', () => {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      return (this.hasAttribute('data-workflow-inspector')
        ? {left: 0, right: 390, top: 400, width: 390, height: 444}
        : {left: 0, right: 390, top: 100, width: 390, height: 744}) as DOMRect;
    });
    render(<><div data-canvas-pane><CanvasReadingView storageKey="mobile" /></div><div data-workflow-inspector /></>);
    expect(graph.setViewport).toHaveBeenLastCalledWith({x: 32, y: 90.5, zoom: 0.85});
  });
  it('compensates selected-node layout movement without changing zoom, and leaves result refreshes alone', () => {
    const component = render(<CanvasReadingView storageKey="anchor" />);
    graph.view = {x: -400, y: 30, zoom: 0.8};graph.setViewport.mockClear();
    graph.nodes = [{...graph.nodes[0], position: {x: 100, y: 50}}];
    component.rerender(<CanvasReadingView storageKey="anchor" />);
    expect(graph.setViewport).toHaveBeenLastCalledWith({x: -480, y: -10, zoom: 0.8});
    graph.setViewport.mockClear();graph.nodes = [{...graph.nodes[0]}];
    component.rerender(<CanvasReadingView storageKey="anchor" />);
    expect(graph.setViewport).not.toHaveBeenCalled();
  });
});
