import { useTranslation } from 'react-i18next';
import { NODE_COLORS, DEFAULT_NODE_COLOR, DEFAULT_NODE_ICON, NODE_ICONS, NODE_LABELS } from '@/pages/canvas/nodes/NODE_TYPES';
import { nodeDescKey, nodeInsertPayload } from './nodeCatalog';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useCanvasViewport } from '@/pages/canvas/CanvasViewportContext';

/**
 * A single base-node card in the Explorer "Nodes" palette (#11). Mirrors
 * other canvas insertion surfaces: drag onto the canvas (MIME `application/vibecanvas-node`,
 * consumed by the canvas `onDrop` → `addNode`) OR click to insert at the
 * viewport center. Both interactions use the shared `nodeInsertPayload`.
 */
export function NodeCard({ nodeType, readOnly }: { nodeType: string; readOnly: boolean }) {
  const { t } = useTranslation();
  const { viewportCenterFlowPos } = useCanvasViewport();
  const addNode = useWorkflowEditStore((s) => s.addNode);
  const color = NODE_COLORS[nodeType] ?? DEFAULT_NODE_COLOR;
  const Icon = NODE_ICONS[nodeType] ?? DEFAULT_NODE_ICON;
  const label = t(`nodes_palette.label.${nodeType}`, NODE_LABELS[nodeType] ?? nodeType);
  const desc = t(nodeDescKey(nodeType), '');

  const onDragStart = (e: React.DragEvent) => {
    if (readOnly) return;
    e.dataTransfer.setData('application/vibecanvas-node', JSON.stringify(nodeInsertPayload(nodeType)));
    e.dataTransfer.effectAllowed = 'move';
  };
  // Double-click to insert (single-click was too easy to mis-trigger). Drag
  // still works for placement.
  const onDoubleClick = () => {
    if (readOnly) return;
    const pos = viewportCenterFlowPos() ?? { x: 0, y: 0 };
    addNode(nodeInsertPayload(nodeType), pos);
  };

  return (
    <button
      type="button"
      data-node-card
      data-node-type={nodeType}
      draggable={!readOnly}
      onDragStart={onDragStart}
      onDoubleClick={onDoubleClick}
      title={desc || label}
      className={`flex min-h-10 w-full items-start gap-2 rounded px-2 py-1.5 text-left ${
        readOnly ? 'opacity-50' : 'cursor-grab hover:bg-surface-hover active:cursor-grabbing'
      }`}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" style={{ color }} aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium leading-4">{label}</span>
        {desc && <span className="block truncate text-xs text-muted-foreground">{desc}</span>}
      </span>
    </button>
  );
}
