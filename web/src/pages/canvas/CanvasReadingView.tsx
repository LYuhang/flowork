import { useEffect, useRef } from 'react';
import { getNodesBounds, getViewportForBounds, useNodes, useNodesInitialized, useReactFlow, useStore, useViewport } from '@xyflow/react';

import { readViewport } from './canvas-viewport-storage';

/** Browser-only presentation state; never written to a workflow version. */
export function CanvasReadingView({ storageKey }: { storageKey: string }) {
  const ready = useNodesInitialized();
  const nodes = useNodes();
  const width = useStore(s => s.width);
  const height = useStore(s => s.height);
  const viewport = useViewport();
  const { getViewport, setViewport } = useReactFlow();
  const marker = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const host = marker.current?.closest("[data-canvas-pane]");
    if (viewport.zoom < 0.6) host?.setAttribute("data-canvas-compact", "true");
    else host?.removeAttribute("data-canvas-compact");
    return () => host?.removeAttribute("data-canvas-compact");
  }, [viewport.zoom]);
  const initialized = useRef(false);
  const previous = useRef(new Map<string, { x: number; y: number }>());
  useEffect(() => {
    if (!ready || !width || !height || !nodes.length) return;
    if (!initialized.current) {
      const canvas = marker.current?.closest('[data-canvas-pane]')?.getBoundingClientRect();
      const inspector = document.querySelector('[data-workflow-inspector]')?.getBoundingClientRect();
      const visibleHeight = canvas && inspector && inspector.left < canvas.right && inspector.right > canvas.left && inspector.top > canvas.top
        ? Math.min(height, inspector.top - canvas.top) : height;
      const saved = readViewport(storageKey);
      const overview = getViewportForBounds(getNodesBounds(nodes), width, visibleHeight, 0.1, 1, 0.16);
      const start = nodes.find(n => n.data.node_type === 'StartNode') ?? nodes[0];
      const zoom = Math.min(0.85, Math.max(0.6, (width - 48) / 224));
      void setViewport(saved ?? (overview.zoom >= 0.6 ? overview : {
        x: 32 - start.position.x * zoom,
        y: visibleHeight / 2 - (start.position.y + (start.measured?.height ?? 140) / 2) * zoom,
        zoom,
      }));
      initialized.current = true;
    } else {
      const anchor = nodes.find(n => n.selected && !n.dragging);
      const before = anchor && previous.current.get(anchor.id);
      if (anchor && before && (before.x !== anchor.position.x || before.y !== anchor.position.y)) {
        const view = getViewport();
        void setViewport({ ...view, x: view.x + (before.x - anchor.position.x) * view.zoom, y: view.y + (before.y - anchor.position.y) * view.zoom });
      }
    }
    previous.current = new Map(nodes.map(n => [n.id, { ...n.position }]));
  }, [ready, width, height, nodes, storageKey, setViewport, getViewport]);
  useEffect(() => {
    if (!initialized.current) return;
    const timer = window.setTimeout(() => {
      try { localStorage.setItem(storageKey, JSON.stringify(getViewport())); } catch { /* Private browsing/storage quota. */ }
    }, 200);
    return () => window.clearTimeout(timer);
  }, [viewport, storageKey, getViewport]);
  return <span ref={marker} hidden />;
}
