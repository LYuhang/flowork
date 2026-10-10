import ELK from 'elkjs/lib/elk-api';
import workerUrl from 'elkjs/lib/elk-worker.min.js?url';
import { Position, type Edge, type InternalNode } from '@xyflow/react';
import { calculateCompactLayout, type LayoutEdge, type LayoutNode, type LayoutPort } from './compact-layout';

export function measuredLayoutInput(nodes: InternalNode[], edges: Edge[]) {
  const measured: LayoutNode[] = nodes.map(node => {
    const { width, height } = node.measured;
    if (!width || !height) throw new Error('Wait for the canvas to finish measuring its nodes.');
    const ports: LayoutPort[] = (['source', 'target'] as const).flatMap(type =>
      (node.internals.handleBounds?.[type] ?? []).map(handle => ({
        id: `${type}:${handle.id ?? ''}`,
        // Match React Flow's edge endpoint on the outer handle boundary.
        x: handle.x + (handle.position === Position.Left ? 0 : handle.position === Position.Right ? handle.width : handle.width / 2),
        y: handle.y + (handle.position === Position.Bottom ? handle.height : handle.height / 2),
        side: handle.position === Position.Left ? 'WEST' as const : handle.position === Position.Bottom ? 'SOUTH' as const : 'EAST' as const,
      })),
    );
    return { id: node.id, width, height, ports };
  });
  const links: LayoutEdge[] = edges.map(edge => ({
    id: edge.id, source: edge.source, target: edge.target,
    sourcePort: `source:${edge.sourceHandle ?? ''}`,
    targetPort: `target:${edge.targetHandle ?? ''}`,
    pairing: Boolean(edge.data?.__pairing__),
  }));
  return { nodes: measured, edges: links };
}

/** Loaded only on request. Each invocation releases its worker, including on navigation. */
export async function runCompactLayout(nodes: InternalNode[], edges: Edge[], viewport: { width: number; height: number }, signal: AbortSignal) {
  signal.throwIfAborted();
  const input = measuredLayoutInput(nodes, edges);
  const engine = new ELK({ workerUrl });
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let abort: () => void = () => {};
  const interrupted = new Promise<never>((_, reject) => {
    abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, { once: true });
    timeout = setTimeout(() => reject(new Error('Layout calculation exceeded 30 seconds.')), 30_000);
  });
  try {
    return await Promise.race([calculateCompactLayout(engine, input.nodes, input.edges, viewport), interrupted]);
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', abort);
    engine.terminateWorker();
  }
}
