import { replaceEqualDeep } from '@tanstack/react-query';
import type { Edge, Node } from '@xyflow/react';
import { layoutOverlappingNodes } from './auto-layout';

// Draft edits clone JSON payloads. Compare by ID so an unchanged graph item
// keeps its reference even after insertion, deletion, reordering or undo.
function retainUnchanged<T extends { id: string }>(previous: T[], incoming: T[]): T[] {
  const byId = new Map(previous.map((item) => [item.id, item]));
  const next = incoming.map((item) => replaceEqualDeep(byId.get(item.id), item));
  return next.length === previous.length && next.every((item, i) => item === previous[i])
    ? previous : next;
}

export function reconcileCanvasNodes(
  previous: Node[], incoming: Node[], edges: Edge[], repairLayout: boolean,
): Node[] {
  const byId = new Map(previous.map((node) => [node.id, node]));
  const refreshed = incoming.map((node) => {
    const existing = byId.get(node.id);
    return existing ? {
      ...node,
      ...('measured' in existing ? { measured: existing.measured } : {}),
      ...('selected' in existing ? { selected: existing.selected } : {}),
      ...('dragging' in existing ? { dragging: existing.dragging } : {}),
    } : node;
  });
  // Repair clean snapshots only; do not persist layout or overwrite dirty edits.
  return retainUnchanged(previous,
    repairLayout ? layoutOverlappingNodes(refreshed, edges) : refreshed);
}

export function reconcileCanvasEdges(previous: Edge[], incoming: Edge[]): Edge[] {
  const byId = new Map(previous.map((edge) => [edge.id, edge]));
  return retainUnchanged(previous, incoming.map((edge) => {
    const existing = byId.get(edge.id);
    return existing && 'selected' in existing ? { ...edge, selected: existing.selected } : edge;
  }));
}
