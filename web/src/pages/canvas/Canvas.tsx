/* eslint-disable react-refresh/only-export-components -- Canvas exports its tested projection/drop contract alongside the render boundary. */
/**
 * xyflow host for the workflow draft. Pure render layer — owns the
 * `ReactFlow` instance but does NOT own workflow truth. The draft
 * lives in `useWorkflowEditStore` (`stores/workflow-edit.ts`), which
 * is seeded by `CanvasPage` from the server snapshot.
 *
 * Workflow → graph adapter
 * ------------------------
 * `workflowDictToNodesEdges` is a pure transform from the legacy
 * "flat dict keyed by node_id" shape into xyflow's `{nodes, edges}`
 * pair. Convention from the legacy code:
 *   - Reserved keys start with `__` (e.g. `__meta__`) and are ignored.
 *   - Each node's `__attributes__.{x,y}` carries UI position, falling
 *     back to (0, 0) — auto-layout (T7) will fix the pile-up.
 *   - `children: string[]` enumerates outgoing directed edges; we emit
 *     them as animated edges so the flow direction is obvious. Loop
 *     back-edges are modeled separately (`loop_begin_node_id`) and are
 *     not part of `children` per the workflow data model — they are
 *     NOT emitted here and will be visualized in a later task.
 *
 * Sync strategy
 * -------------
 * `useNodesState` / `useEdgesState` give us xyflow's recommended local
 * controlled state with native drag support, but the store remains
 * authoritative. On every store-draft change we recompute and write
 * the local nodes/edges arrays. Position drag-end events are lifted
 * back into `applyEdit` so the in-memory draft tracks node `(x, y)`;
 * see {@link handleNodesChange}.
 *
 * CSS: `@xyflow/react/dist/style.css` must be imported once globally —
 * we do it here at module scope.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react';
import { PanelLeftOpen } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import {
  CustomNode,
  LOOP_BACK_SOURCE_HANDLE_ID,
  LOOP_BACK_TARGET_HANDLE_ID,
  type NodePayload,
} from '@/pages/canvas/nodes/CustomNode';
import { nodeWarnings } from '@/pages/canvas/nodeWarnings';
import {
  useWorkflowEditStore,
  wouldCreateCycle,
  type WorkflowDraft,
} from '@/stores/workflow-edit';
import { useUIStore } from '@/stores/ui';
import { useRegisterViewportCenter } from '@/pages/canvas/CanvasViewportContext';
import { Button } from '@/components/ui/button';

const nodeTypes = { custom: CustomNode };

/** Semantic focus color for config-derived Parallel/Loop pairing edges. */
const PAIRING_EDGE_COLOR = 'oklch(var(--focus))';

/** Pure: flat-dict workflow → xyflow nodes/edges. Exported for tests later. */
export function workflowDictToNodesEdges(
  wf: Record<string, unknown> | null,
): { nodes: Node[]; edges: Edge[] } {
  if (!wf) return { nodes: [], edges: [] };

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Cheap LOCAL validity warnings (empty condition_str, conditions≠children,
  // unpaired Parallel/Loop, dangling ref). Computed once here so each
  // CustomNode receives its own `__warnings__` (string[] of i18n keys) via
  // `data` — no extra context plumbing. See `nodeWarnings.ts`.
  const warnings = nodeWarnings(wf as Record<string, unknown>);

  for (const [key, value] of Object.entries(wf)) {
    if (key.startsWith('__')) continue;
    const payload = (value ?? {}) as NodePayload;
    const attrs = payload.__attributes__ ?? {};
    nodes.push({
      id: key,
      type: 'custom',
      position: { x: attrs.x ?? 0, y: attrs.y ?? 0 },
      data: {
        ...(payload as unknown as Record<string, unknown>),
        __warnings__: warnings.get(key) ?? [],
      },
    });
    const children = Array.isArray(payload.children) ? payload.children : [];
    for (const child of children) {
      edges.push({
        id: `${key}->${child}`,
        source: key,
        target: child,
        animated: true,
        label: edgeLabelFor(payload, child),
      });
    }

    // Pairing edge (Stream 8 A1): when a Parallel/Loop START holds a set
    // partner pointer, emit an ADDITIONAL distinct dashed/labeled edge to its
    // END so the pairing is VISIBLE on canvas though it is NOT a child edge.
    // These are config-derived: non-deletable + non-selectable so they cannot
    // be confused with (or mutate) the `children` graph.
    const pairing = pairingEdgeFor(key, payload);
    if (pairing) edges.push(pairing);
  }

  return { nodes, edges };
}

/**
 * Build the config-derived pairing edge for a Loop BEGIN node, or `null`.
 *
 * ONLY loops draw a pairing edge: the Loop's begin↔end pairing carries an
 * implicit *jump-back* (the end node loops control flow to the begin node), so
 * the dashed back-edge is meaningful. Parallel start/end are paired the same
 * way in config, but there is no jump — the partner is reached through the
 * ordinary `children` graph — so a pairing edge there is just visual noise and
 * is intentionally NOT emitted.
 *
 * Derived from the BEGIN side's partner pointer (`loop_end_node_id`) so we
 * never emit it twice and it is re-derived from config on every render (moving
 * or selecting nodes can't break it). The edge is flagged
 * `data.__pairing__: true` (a non-`children` marker the canvas uses to keep it
 * out of disconnect handling) and is non-deletable + non-selectable.
 *
 * Routing: the edge represents the loop-back JUMP, so it runs from the BOTTOM
 * of the LoopEnd node (`source`) to the BOTTOM of the LoopBegin node
 * (`target`) — i.e. source/target are flipped to end→begin vs the config
 * pointer — attaching to the DEDICATED, non-connectable bottom handles
 * (`loop-back-source` / `loop-back-target`, rendered only on Loop boundaries by
 * `CustomNode`). A `smoothstep` path between two BOTTOM handles routes with
 * right-angles that dip DOWNWARD and back up, so the back-edge sits under the
 * node row and never hides beneath the left→right data flow. (smoothstep is the
 * cleanest of the built-ins here: two bottom anchors make it drop below both
 * nodes; a plain bezier would bow but can still clip a node sitting between
 * them, so we prefer the orthogonal dip. Best-effort — extreme layouts may
 * still graze a node, but it no longer overlaps the begin/end pair.)
 */
function pairingEdgeFor(id: string, payload: NodePayload): Edge | null {
  const cfg = payload.node_config as Record<string, unknown> | undefined;
  if (!cfg) return null;
  if (payload.node_type !== 'LoopBeginNode') return null;
  const endId: unknown = cfg.loop_end_node_id;
  const label = 'loop';
  if (typeof endId !== 'string' || !endId) return null;
  return {
    // id keeps the begin→end namespace (one stable edge per pair) even though
    // the rendered direction is end→begin.
    id: `pair:${id}->${endId}`,
    source: endId,
    target: id,
    sourceHandle: LOOP_BACK_SOURCE_HANDLE_ID,
    targetHandle: LOOP_BACK_TARGET_HANDLE_ID,
    type: 'smoothstep',
    label,
    animated: false,
    selectable: false,
    deletable: false,
    data: { __pairing__: true },
    style: {
      stroke: PAIRING_EDGE_COLOR,
      strokeDasharray: '6 4',
      strokeWidth: 1.5,
    },
    labelStyle: { fill: PAIRING_EDGE_COLOR, fontWeight: 600 },
  };
}

/**
 * Label an out-edge with its branch name for a Condition/ParallelStart
 * source so multiple branches are distinguishable on the canvas. Matched
 * by `next_node_id === child` (rename-safe), never by name. Returns
 * `undefined` for ordinary sources (xyflow omits the label).
 */
function edgeLabelFor(
  payload: NodePayload,
  child: string,
): string | undefined {
  const cfg = payload.node_config as Record<string, unknown> | undefined;
  if (!cfg) return undefined;
  if (payload.node_type === 'ConditionNode' && Array.isArray(cfg.conditions)) {
    const row = (cfg.conditions as Record<string, unknown>[]).find(
      (c) => c.next_node_id === child,
    );
    return row?.condition_name as string | undefined;
  }
  if (
    payload.node_type === 'ParallelStartNode' &&
    cfg.branches &&
    typeof cfg.branches === 'object'
  ) {
    for (const [name, b] of Object.entries(
      cfg.branches as Record<string, Record<string, unknown>>,
    )) {
      if (b.next_node_id === child) return name;
    }
  }
  return undefined;
}

/**
 * Pure drop handler for the template palette drag-and-drop. Extracted from
 * the component so it can be unit-tested without rendering the whole xyflow
 * host (the full render fights mocked `useNodesState`/`useEdgesState`).
 *
 * Reads the `application/vibecanvas-node` payload the palette card sets on
 * `dragstart`, maps the cursor screen position into flow coords, and inserts
 * a node via the edit store. No-ops in `readOnly` mode, when there is no
 * payload, or when the payload is not valid JSON.
 */
export function handleNodeDrop(
  e: { dataTransfer: DataTransfer; clientX: number; clientY: number; preventDefault: () => void },
  opts: {
    readOnly: boolean;
    screenToFlowPosition: (p: { x: number; y: number }) => { x: number; y: number };
    addNode: (payload: Record<string, unknown>, pos: { x: number; y: number }) => void;
  },
): void {
  if (opts.readOnly) return;
  e.preventDefault();
  const raw = e.dataTransfer.getData('application/vibecanvas-node');
  if (!raw) return;
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(raw);
  } catch {
    return;
  }
  opts.addNode(payload, opts.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
}

export interface CanvasProps {
  /**
   * When true, the canvas is mounted in read-only mode (T14: pinned
   * historical version). Nodes can still be *selected* (so the inspector
   * surfaces their state) but cannot be dragged, connected, or position-
   * edited via `applyEdit`. Defaults to `false` so existing call sites
   * stay unchanged.
   */
  readOnly?: boolean;
}

export function Canvas({ readOnly = false }: CanvasProps = {}) {
  const { t } = useTranslation();
  const draft = useWorkflowEditStore((s) => s.draft);
  const applyEdit = useWorkflowEditStore((s) => s.applyEdit);
  const addNode = useWorkflowEditStore((s) => s.addNode);
  const connectNodes = useWorkflowEditStore((s) => s.connectNodes);
  const disconnectNodes = useWorkflowEditStore((s) => s.disconnectNodes);
  const removeNodes = useWorkflowEditStore((s) => s.removeNodes);
  const setCanvasInteracting = useUIStore((s) => s.setCanvasInteracting);
  const setInspectorOpen = useUIStore((s) => s.setInspectorOpen);
  const setExplorerOpen = useUIStore((s) => s.setExplorerOpen);
  const { screenToFlowPosition } = useReactFlow();
  const wrapperRef = useRef<HTMLDivElement>(null);
  const registerViewportCenter = useRegisterViewportCenter();

  // Publish the live "viewport center in flow coords" getter to the ancestor
  // CanvasViewportProvider (in AppLayout) so the Explorer's node/template
  // palette cards — siblings of the canvas, NOT descendants — can place a
  // double-click-added node at the visible center instead of the flow origin.
  useEffect(() => {
    registerViewportCenter(() => {
      const r = wrapperRef.current?.getBoundingClientRect();
      if (!r) return null;
      return screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
    });
  }, [registerViewportCenter, screenToFlowPosition]);

  const { nodes: initialNodes, edges: initialEdges } = useMemo(
    () => workflowDictToNodesEdges(draft),
    [draft],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  // When the draft changes (e.g. server load, undo/redo, vibe action),
  // refresh xyflow's local state.
  //
  // Preserve xyflow's transient UI state (selected, dragging) across
  // draft refreshes so the inspector keeps its selection while the user
  // edits, and a node being dragged isn't visually snapped back.
  useEffect(() => {
    setNodes((prev) => {
      const prevById = new Map(prev.map((n) => [n.id, n]));
      return initialNodes.map((n) => {
        const existing = prevById.get(n.id);
        if (!existing) return n;
        return { ...n, selected: existing.selected, dragging: existing.dragging };
      });
    });
    setEdges((prev) => {
      const prevById = new Map(prev.map((e) => [e.id, e]));
      return initialEdges.map((e) => {
        const existing = prevById.get(e.id);
        if (!existing) return e;
        return { ...e, selected: existing.selected };
      });
    });
  }, [initialNodes, initialEdges, setNodes, setEdges]);

  // Forward xyflow node changes to the local controlled state, then lift
  // position-drag releases into the store so `__attributes__.{x,y}`
  // tracks the user's gesture and the draft becomes dirty.
  //
  // xyflow fires `position` changes with `dragging: true` continuously
  // while the user drags, and exactly one final change with
  // `dragging: false` when the pointer is released. Acting only on the
  // release keeps `undoStack` from filling with one entry per pointer
  // tick.
  const handleNodesChange = useCallback(
    (changes: NodeChange[]) => {
      onNodesChange(changes);
      for (const change of changes) {
        // Suppress hover cards during a node drag. xyflow
        // emits `position` changes with `dragging: true` continuously while
        // the pointer moves and a single `dragging: false` on release, so we
        // mirror that boolean straight into the UI store.
        if (change.type === 'position' && typeof change.dragging === 'boolean') {
          setCanvasInteracting(change.dragging);
        }
        if (
          change.type === 'position' &&
          change.position &&
          change.dragging === false
        ) {
          const { id, position } = change;
          applyEdit((wf) => {
            const node = wf[id];
            if (node && typeof node === 'object') {
              const record = node as Record<string, unknown>;
              const prevAttrs =
                (record.__attributes__ as Record<string, unknown> | undefined) ??
                {};
              record.__attributes__ = {
                ...prevAttrs,
                x: position.x,
                y: position.y,
              };
            }
            return wf;
          });
        }
      }
    },
    [onNodesChange, applyEdit, setCanvasInteracting],
  );

  // Suppress hover cards for the full duration of an edge drag.
  const onConnectStart = useCallback(
    () => setCanvasInteracting(true),
    [setCanvasInteracting],
  );
  const onConnectEnd = useCallback(
    () => setCanvasInteracting(false),
    [setCanvasInteracting],
  );

  // Handle-drag commit: push the edge into the source's `children` via the
  // store (type-aware). We do NOT also call xyflow `addEdge` — the re-sync
  // effect re-derives the edge from the draft (the draft is the only truth).
  const onConnect = useCallback(
    (params: Connection) => {
      if (readOnly || !params.source || !params.target) return;
      connectNodes(params.source, params.target);
    },
    [readOnly, connectNodes],
  );

  // Instant client-side guard (the authoritative check stays the server
  // Check). Rejects self-loop, duplicate, and any edge that would create a
  // `children` cycle (DFS target→source over the live draft). A diamond /
  // fan-in is allowed — only a true cycle is blocked. This also blocks a
  // LoopEnd→LoopBegin child edge (loop-back is config, not a child edge).
  const isValidConnection = useCallback(
    (conn: Connection | Edge): boolean => {
      if (readOnly) return false;
      const { source, target } = conn;
      if (!source || !target) return false;
      if (source === target) return false;
      const wf = useWorkflowEditStore.getState().draft as WorkflowDraft | null;
      if (!wf) return true;
      const src = wf[source] as Record<string, unknown> | undefined;
      const children = Array.isArray(src?.children)
        ? (src!.children as string[])
        : [];
      if (children.includes(target)) return false;
      if (wouldCreateCycle(wf, source, target)) return false;
      return true;
    },
    [readOnly],
  );

  // Lift xyflow edge 'remove' changes (e.g. Backspace on a selected edge)
  // into the store so the deletion persists (the draft is the truth; the
  // edge id is `${src}->${tgt}` but we parse from the live edge's
  // source/target, not the id string).
  const handleEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      onEdgesChange(changes);
      if (readOnly) return;
      for (const change of changes) {
        if (change.type === 'remove') {
          const edge = edges.find((e) => e.id === change.id);
          // Config-derived pairing edges are non-deletable view-only — never
          // route them into `disconnectNodes` (they are not child edges).
          if (edge && (edge.data as { __pairing__?: boolean })?.__pairing__) {
            continue;
          }
          if (edge?.source && edge?.target) {
            disconnectNodes(edge.source, edge.target);
          }
        }
      }
    },
    [onEdgesChange, readOnly, edges, disconnectNodes],
  );

  // Backspace/Delete on selected node(s) → remove from the draft (strips
  // all back-refs in one undo step).
  const onNodesDelete = useCallback(
    (deleted: Node[]) => {
      if (readOnly || deleted.length === 0) return;
      removeNodes(deleted.map((n) => n.id));
    },
    [readOnly, removeNodes],
  );

  // FIX UX-15: double-clicking the BLANK pane toggles the Inspector — if it's
  // open it CLOSES (a quick way to reclaim canvas width), otherwise it opens
  // (workflow scope). A double-click on a NODE is handled separately by
  // `onNodeDoubleClick` (always opens for that node's scope) and must NOT close
  // the sider, so we ignore double-clicks whose target sits inside a node/edge.
  // We detect the pane by walking up from the event target: anything inside
  // `.react-flow__node` / `.react-flow__edge` is NOT the pane.
  const onWrapperDoubleClick = useCallback(
    (e: React.MouseEvent) => {
      const onElement = (e.target as HTMLElement | null)?.closest?.(
        '.react-flow__node, .react-flow__edge',
      );
      if (onElement) return; // node/edge double-click → handled by onNodeDoubleClick
      // Blank-pane double-click.
      if (useUIStore.getState().inspectorOpen) {
        setInspectorOpen(false);
      } else {
        setInspectorOpen(true);
      }
    },
    [setInspectorOpen],
  );

  const onDragOver = (e: React.DragEvent) => {
    if (readOnly) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  };
  const onDrop = (e: React.DragEvent) =>
    handleNodeDrop(e, { readOnly, screenToFlowPosition, addNode });

  return (
    <div
      ref={wrapperRef}
      data-canvas-pane
      className="relative h-full w-full"
      role="region"
      aria-label={t('canvas.workflowCanvas', 'Workflow canvas')}
      onDragOver={onDragOver}
      onDrop={onDrop}
      // Double-click handling (zoomOnDoubleClick is off so we own it):
      //   - on a NODE → `onNodeDoubleClick` opens the Inspector (node scope);
      //   - on the BLANK pane → toggle the Inspector (open if closed, CLOSE if
      //     already open — UX-15) via `onWrapperDoubleClick`, which ignores
      //     double-clicks that landed on a node/edge.
      onDoubleClick={onWrapperDoubleClick}
    >
      <ReactFlow
        // Edge labels are informational.  Let pointer events pass through them
        // so an overlapping branch/loop label cannot make the node beneath it
        // impossible to select (the edge path remains selectable).
        className="workflow-canvas [&_.react-flow__edge-text]:pointer-events-none [&_.react-flow__edge-textbg]:pointer-events-none"
        nodes={nodes}
        edges={edges}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onConnect={onConnect}
        onConnectStart={onConnectStart}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        onNodesDelete={onNodesDelete}
        onNodeDoubleClick={() => setInspectorOpen(true)}
        zoomOnDoubleClick={false}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.16, minZoom: 0.1, maxZoom: 1 }}
        // Large agent-built workflows need a genuine overview scale. Users can
        // zoom back in or use Fit View; 10% keeps even long graphs navigable.
        minZoom={0.1}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
      >
        <Background gap={20} />
        <Controls
          position="bottom-left"
          className="!overflow-hidden !rounded-md !border !border-edge-structural !bg-surface-raised !shadow-none"
        />
        <MiniMap
          position="bottom-right"
          pannable
          zoomable
          className="!hidden !rounded-md !border !border-edge-structural !bg-surface-sunken !shadow-none lg:!block"
        />
      </ReactFlow>

      {nodes.length === 0 && (
        <div
          data-canvas-empty-state
          className="pointer-events-none absolute inset-0 flex items-center justify-center p-6"
        >
          <div className="max-w-md px-8 py-6 text-center">
            <p className="text-base font-semibold text-content-primary">
              {t('canvas.emptyTitle', 'Blank workflow canvas')}
            </p>
            <p className="mt-1 text-sm leading-5 text-content-secondary">
              {readOnly
                ? t('canvas.emptyReadOnly', 'This workflow version has no nodes.')
                : t(
                    'canvas.emptyState',
                    'Open File Explorer to add nodes, or ask the Agent to build the workflow.',
                  )}
            </p>
            {!readOnly && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="pointer-events-auto mt-4"
                onClick={() => setExplorerOpen(true)}
              >
                <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
                {t('canvas.openExplorer', 'Open File Explorer')}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
