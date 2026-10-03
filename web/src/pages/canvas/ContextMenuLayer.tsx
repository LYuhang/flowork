/**
 * Canvas right-click layer (T15.5).
 *
 * Wraps an arbitrary subtree (typically `<Canvas />`) in a shadcn
 * `ContextMenu` so right-clicking anywhere on the canvas opens an
 * action sheet. Items mutate the workflow draft and are hidden in `readOnly`
 * mode (T14 pinned versions).
 *
 * Capturing the right-click coord
 * -------------------------------
 * Radix's `ContextMenu` swallows the `contextmenu` event before we can
 * attach a handler downstream, so we listen at the `ContextMenuTrigger`
 * level and stash `{clientX, clientY}` in local state. Paste converts those
 * screen coordinates into flow coordinates using the live pan/zoom.
 *
 * Selection-aware items
 * ---------------------
 * "Copy selected node" and "Delete selected edge" need to know what xyflow
 * currently considers selected. We read via `useNodes()` / `useEdges()` so the
 * menu re-renders correctly when selection changes between right-clicks. Both
 * rely on being inside the page-level `ReactFlowProvider` (see
 * `CanvasPage.tsx`).
 */
import { useRef, useState, type ReactNode } from 'react';
import { useNodePicker } from './NodePickerContext';
import { Plus, Copy, ClipboardPaste, Trash2, WandSparkles, Quote } from 'lucide-react';
import { useOpenCanvasChat, useCanvasChatReference } from './CanvasChatContext';
import type { components } from '@/lib/api/schema';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { useEdges, useNodes, useReactFlow } from '@xyflow/react';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

export interface ContextMenuLayerProps {
  children: ReactNode;
  /**
   * Hide mutating items when the canvas is in pinned read-only mode.
   */
  readOnly?: boolean;
}

export function ContextMenuLayer({
  children,
  readOnly = false,
}: ContextMenuLayerProps) {
  const { t } = useTranslation();
  const showNodePicker = useNodePicker();
  const openChat = useOpenCanvasChat();
  const reference = useCanvasChatReference();
  const addingReference = useRef(false);
  const [chatTarget, setChatTarget] = useState<components['schemas']['WorkflowChatTarget'] | null>(null);
  const openingChat = useRef(false);
  const openingNodePicker = useRef(false);
  // Screen coords of the right-click that opened the menu — used by Paste so a
  // pasted node lands where the user clicked.
  const [menuCoord, setMenuCoord] = useState<{ x: number; y: number } | null>(
    null,
  );

  const nodes = useNodes();
  const edges = useEdges();
  const { screenToFlowPosition } = useReactFlow();
  const disconnectNodes = useWorkflowEditStore((s) => s.disconnectNodes);
  const removeNode = useWorkflowEditStore((s) => s.removeNode);
  const copyNodes = useWorkflowEditStore((s) => s.copyNodes);
  const pasteNodes = useWorkflowEditStore((s) => s.pasteNodes);
  const clipboard = useWorkflowEditStore((s) => s.clipboard);

  const selectedNode = nodes.find((n) => n.selected);
  const selectedEdge = edges.find((e) => e.selected);

  const handleContextMenu = (e: React.MouseEvent | React.PointerEvent) => {
    setMenuCoord({ x: e.clientX, y: e.clientY });
    const element = e.target instanceof Element ? e.target : null;
    const nodeId = element?.closest('.react-flow__node')?.getAttribute('data-id');
    const edgeId = element?.closest('.react-flow__edge')?.getAttribute('data-id');
    const edge = edgeId ? edges.find(item => item.id === edgeId) : undefined;
    setChatTarget(nodeId ? { kind: 'node', node_id: nodeId }
      : edge && !edge.id.startsWith('pair:') ? { kind: 'edge', source: edge.source, target: edge.target,
        source_handle: edge.sourceHandle, target_handle: edge.targetHandle }
      : edgeId ? null : { kind: 'workflow' });
  };

  const onDeleteSelectedEdge = () => {
    if (!selectedEdge) return;
    // Persist the deletion in the draft (the source of truth). The re-sync
    // effect in Canvas re-derives edges from `children[]`, so removing the
    // child here makes the edge disappear without touching xyflow state.
    disconnectNodes(selectedEdge.source, selectedEdge.target);
  };

  const onDeleteSelectedNode = () => {
    if (!selectedNode) return;
    removeNode(selectedNode.id);
  };

  const onCopySelectedNode = () => {
    if (!selectedNode) return;
    copyNodes([selectedNode.id]);
    toast.success(
      t('contextMenu.toast.copiedNode', 'Copied node {{id}}', { id: selectedNode.id }),
    );
  };

  // Paste at the right-click coord (converted to flow space via the live
  // pan/zoom) so the duplicate lands where the user clicked; fall back to
  // the flow origin if the menu has no captured coord.
  const onPaste = () => {
    if (clipboard.length === 0) return;
    const anchor = menuCoord
      ? screenToFlowPosition({ x: menuCoord.x, y: menuCoord.y })
      : { x: 0, y: 0 };
    pasteNodes(anchor);
  };

  return (
    <ContextMenu>
        <ContextMenuTrigger asChild onContextMenu={handleContextMenu}
          // Radix opens touch/pen menus from a long-press timer, without a
          // contextmenu event. Capture the target before that timer opens it.
          onPointerDownCapture={handleContextMenu}>
          <div className="h-full w-full">{children}</div>
        </ContextMenuTrigger>
        {!readOnly && (
          <ContextMenuContent data-role="canvas-context-menu" className="min-w-56" onCloseAutoFocus={(event) => {
            if (addingReference.current) {
              event.preventDefault();
              addingReference.current = false;
              const target = chatTarget;
              if (target) requestAnimationFrame(() => { void reference?.add(target); });
              return;
            }
            if (openingChat.current) {
              event.preventDefault();
              openingChat.current = false;
              const target = chatTarget;
              if (target && menuCoord) requestAnimationFrame(() => openChat?.(target, menuCoord));
              return;
            }
            if (!openingNodePicker.current) return;
            event.preventDefault();
            openingNodePicker.current = false;
            const anchor = menuCoord ?? undefined;
            requestAnimationFrame(() => showNodePicker(anchor));
          }}>
            <ContextMenuItem data-action="context-add-node" onSelect={() => {
              // Let the menu release its focus trap before opening the picker.
              openingNodePicker.current = true;
            }}><Plus className="mr-2 h-4 w-4" />{t('nodePicker.add', 'Add node')}</ContextMenuItem>
            {openChat && <ContextMenuItem data-action="context-chat" disabled={!chatTarget}
              onSelect={() => { openingChat.current = true; }}>
              <WandSparkles className="mr-2 h-4 w-4" />{t('canvasChat.here', 'Chat here')}
            </ContextMenuItem>}
            {reference && <ContextMenuItem data-action="context-reference" disabled={!chatTarget || !reference.enabled}
              onSelect={() => { addingReference.current = true; }}>
              <Quote className="mr-2 h-4 w-4" />{t('canvasChat.reference', 'Reference as context')}
            </ContextMenuItem>}
            <ContextMenuSeparator />
              <ContextMenuItem
                disabled={!selectedNode}
                onSelect={onCopySelectedNode}
                data-action="context-copy-node"
              >
                <Copy className="mr-2 h-4 w-4" />
                {t('contextMenu.copyNode', 'Copy node')}
              </ContextMenuItem>
              <ContextMenuItem
                disabled={clipboard.length === 0}
                onSelect={onPaste}
                data-action="context-paste"
              >
                <ClipboardPaste className="mr-2 h-4 w-4" />
                {t('contextMenu.pasteNode', 'Paste node')}
              </ContextMenuItem>
              <ContextMenuSeparator />
              <ContextMenuItem
                disabled={!selectedNode}
                onSelect={onDeleteSelectedNode}
                className="text-destructive focus:text-destructive"
                data-action="context-delete-node"
              >
                <Trash2 className="mr-2 h-4 w-4" />
                {t('contextMenu.deleteNode', 'Delete node')}
              </ContextMenuItem>
              <ContextMenuItem
                disabled={!selectedEdge}
                onSelect={onDeleteSelectedEdge}
                className="text-destructive focus:text-destructive"
                data-action="context-delete-edge"
              >
                <Trash2 className="mr-2 h-4 w-4" />
                {t('contextMenu.deleteEdge', 'Delete selected edge')}
              </ContextMenuItem>
          </ContextMenuContent>
        )}
    </ContextMenu>
  );
}
