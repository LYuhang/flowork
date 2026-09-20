/* eslint-disable react-refresh/only-export-components -- The node renderer owns shared handle geometry used by the canvas projection. */
/**
 * Generic per-node renderer used for ALL 14 (currently 15) workflow node
 * types — the legacy frontend already collapsed type-specific rendering
 * into a single Svelte component (`frontend/CustomNode.svelte`); we
 * preserve that decision in React form. Per-type editing lives in the
 * right inspector (T8 / T8.5), not the canvas card.
 *
 * Layout (compact, uniform card — n8n / LangFlow / Coze style):
 *   - Header bar — a per-type icon (`NODE_ICONS`) + the node NAME (truncated)
 *     + a small type badge, tinted with `NODE_COLORS[node_type]` (graceful
 *     fallback). The header carries the exec/warning indicators.
 *   - Body — a COMPACT in/out summary STACKED VERTICALLY (a short `Inputs`
 *     row then an `Outputs` row, each with a muted count pill) instead of an
 *     inline field list, so the card height is ~CONSTANT regardless of how
 *     many fields the node has and nothing clips at the fixed width. The full
 *     field detail lives in the hover peek-card and the right Inspector.
 *   - Fixed width (`w-56`) so nodes of every type read as a uniform grid.
 *
 * Handles:
 *   - `target` handle at top — present on every node EXCEPT `StartNode`,
 *     since nothing flows into the start.
 *   - `source` handle at bottom — present on every node EXCEPT `EndNode`,
 *     since nothing flows out of the end.
 *   - `Parallel*` / `Loop*` boundaries deliberately get the default
 *     handle set; the parallel/loop pairing constraint lives in the
 *     backend validator (`core.workflow.Workflow.check`), not the
 *     canvas. T7+ may add visual cues; for T6 the card is symmetric.
 *
 * a11y: each card publishes `aria-label="{node_type} {node_name}"` so
 * screen readers and Playwright selectors can find it.
 */
import { memo } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { cn } from '@/lib/utils';
import {
  DEFAULT_NODE_COLOR,
  DEFAULT_NODE_ICON,
  NODE_COLORS,
  NODE_ICONS,
  NODE_LABELS,
} from '@/pages/canvas/nodes/NODE_TYPES';
import { NodeHoverCard } from '@/pages/canvas/nodes/NodeHoverCard';
import { NodeOutputPreview } from '@/pages/canvas/nodes/NodeOutputPreview';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useUIStore } from '@/stores/ui';

/**
 * Return the at-a-glance execution state for this node, narrowed from the raw
 * per-node EXEC_UPDATE `status` string (backend emits `running` /
 * `completed` / `error`; any other terminal value the engine reports — e.g.
 * `skipped` — is treated as inert so it never lights a ring). Returned as a
 * tiny union the card renders a ring + icon from.
 */
export type NodeExecState = 'running' | 'completed' | 'error' | null;

function narrowExecState(status: string | undefined): NodeExecState {
  if (status === 'running' || status === 'completed' || status === 'error') {
    return status;
  }
  return null;
}

/**
 * Pure suppression rule for the node hover card.
 * Exported so the wiring is unit-testable without rendering Radix. The card
 * must NOT open while:
 *   - a canvas gesture is in flight (`canvasInteracting` — a node drag or an
 *     edge connect, both flipped by `Canvas.tsx`); OR
 *   - THIS node is already open in the Inspector — i.e. the node is selected
 *     AND the inspector scope is NOT the workflow override (a node-selected +
 *     non-`workflow` scope means the node-scope tabs are showing this node, so
 *     the peek card is redundant).
 */
export function nodeHoverSuppressed(args: {
  canvasInteracting: boolean;
  selected: boolean;
  inspectorScope: 'auto' | 'workflow';
}): boolean {
  const nodeOpenInInspector =
    args.selected && args.inspectorScope !== 'workflow';
  return args.canvasInteracting || nodeOpenInInspector;
}

/** Ring colour for the outer card per execution state. */
const EXEC_RING_CLASS: Record<'running' | 'completed' | 'error', string> = {
  // Mid-flight: a calm BREATHING blue halo (custom `animate-node-breathe`
  // keyframe — a swelling/receding box-shadow ~1.6s ease-in-out, not the harsh
  // `animate-pulse` opacity blink). The animation IS the glow, so no static
  // ring class here; each running node's halo is its own element, so multiple
  // parallel branches breathe independently (in a shared CSS rhythm).
  running: 'border-state-running motion-safe:animate-node-breathe',
  completed: 'border-state-success',
  error: 'border-state-danger',
};

export interface NodePayload {
  node_id?: string;
  node_name?: string;
  node_type?: string;
  node_description?: string;
  children?: string[];
  input_fields?: Record<string, unknown>;
  output_fields?: Record<string, unknown>;
  node_config?: Record<string, unknown>;
  __attributes__?: { x?: number; y?: number };
  /**
   * Cheap LOCAL validity warning i18n keys for this node, injected by
   * `workflowDictToNodesEdges` from the `nodeWarnings(draft)` selector. Empty /
   * absent ⟺ no warnings. The badge tooltip resolves each via `t()`.
   */
  __warnings__?: string[];
}

/**
 * Shared handle classes. The visible dot stays ~8px, but the hit-area is
 * enlarged (a larger transparent pad via `::after`) and the dot GROWS + turns
 * `crosshair` on node-hover (the `.group` is the card) so non-technical users
 * discover drag-to-connect. xyflow's connection logic is unchanged — only the
 * styling.
 */
export const HANDLE_CLASS = cn(
  '!h-2 !w-2 !border !border-background !bg-muted-foreground',
  'after:absolute after:left-1/2 after:top-1/2 after:h-6 after:w-6',
  'after:-translate-x-1/2 after:-translate-y-1/2 after:content-[""]',
  'cursor-crosshair transition-transform duration-feedback',
  'group-hover:!h-3 group-hover:!w-3 group-hover:!bg-primary',
);

/**
 * The dedicated bottom handle the config-derived Loop-back edge attaches to.
 * Distinct from the data handles: tinted with the semantic focus color, no
 * crosshair / hover-grow, and rendered ONLY on Loop boundary nodes. These are
 * `isConnectable={false}` so a user can neither start nor land a manual
 * connection here — they exist purely as a programmatic attach point for the
 * end-bottom → begin-bottom loop-back edge (see `pairingEdgeFor` in Canvas).
 */
export const LOOP_BACK_HANDLE_CLASS =
  '!h-2 !w-2 !rounded-full !border !border-background !bg-focus !cursor-default';

/** Stable handle ids for the Loop-back pairing edge attach points. */
export const LOOP_BACK_SOURCE_HANDLE_ID = 'loop-back-source';
export const LOOP_BACK_TARGET_HANDLE_ID = 'loop-back-target';

function CustomNodeImpl({ data, selected, id }: NodeProps) {
  const { t } = useTranslation();
  const payload = (data ?? {}) as NodePayload;
  const nodeType = payload.node_type ?? 'UnknownNode';
  const headerColor = NODE_COLORS[nodeType] ?? DEFAULT_NODE_COLOR;
  const label = NODE_LABELS[nodeType] ?? nodeType;
  const Icon = NODE_ICONS[nodeType] ?? DEFAULT_NODE_ICON;
  const title = payload.node_name ?? payload.node_id ?? id;
  const isStart = nodeType === 'StartNode';
  const isEnd = nodeType === 'EndNode';
  // Loop boundaries carry an EXTRA non-connectable bottom handle so the
  // config-derived loop-back edge can route below the node row (end→begin).
  const isLoopBegin = nodeType === 'LoopBeginNode';
  const isLoopEnd = nodeType === 'LoopEndNode';
  // Compact in/out COUNTS — the card shows counts only (constant height); the
  // field detail lives in the hover peek-card + the right Inspector.
  const inputCount = payload.input_fields
    ? Object.keys(payload.input_fields).length
    : 0;
  const outputCount = payload.output_fields
    ? Object.keys(payload.output_fields).length
    : 0;
  const warnings = Array.isArray(payload.__warnings__)
    ? payload.__warnings__
    : [];

  // Subscribe only to this node's execution status so a
  // running→completed transition on a sibling never re-renders this memo'd
  // card). The error message is pulled lazily — only when the node is in the
  // error state — to keep the selector output a primitive string.
  const execStatusRaw = useExecStreamStore((s) => s.perNode[id]?.status);
  const execState = narrowExecState(execStatusRaw);
  const execError = useExecStreamStore((s) =>
    execState === 'error' ? s.perNode[id]?.error : undefined,
  );
  const execResult = useExecStreamStore((s) =>
    execState === 'completed' ? s.perNode[id]?.result : undefined,
  );

  // Do not open the hover card while a canvas gesture is in
  // flight (drag/connect), nor when THIS node is already open in the
  // Inspector (selected + the scope is NOT the workflow override → the
  // node-scope tabs are showing this node, so the card is redundant).
  const canvasInteracting = useUIStore((s) => s.canvasInteracting);
  const inspectorScope = useUIStore((s) => s.inspectorScope);
  const suppressed = nodeHoverSuppressed({
    canvasInteracting,
    selected: Boolean(selected),
    inspectorScope,
  });

  const resolvedWarnings = warnings.map((w) => t(w));

  return (
    <div className="w-56">
    <NodeHoverCard
      title={title}
      typeLabel={label}
      description={payload.node_description}
      execState={execState}
      execResult={execResult}
      execError={execError}
      warnings={resolvedWarnings}
      inputCount={inputCount}
      outputCount={outputCount}
      suppressed={suppressed || nodeType === 'TemplateNode'}
    >
      <div
        aria-label={`${nodeType} ${payload.node_name ?? ''}`.trim()}
        data-exec-state={execState ?? undefined}
        className={cn(
          'group relative w-56 overflow-hidden rounded-md border border-edge-structural bg-surface-raised text-content-primary transition-colors duration-feedback',
          // Execution ring wins visually over the selection ring while a run is
          // live/terminal; selection ring applies only when idle (no exec state).
          execState
            ? EXEC_RING_CLASS[execState]
            : selected && 'border-focus',
        )}
      >
        {!isStart && (
          <Handle type="target" position={Position.Left} className={HANDLE_CLASS} />
        )}

        {/* Header — per-type icon + node NAME (truncated) + a small type badge.
            Tinted with the per-type accent color; carries the at-a-glance
            exec/warning indicators. The header height is constant. */}
        <div
          data-node-header
          className="flex items-center gap-1.5 border-b px-2.5 py-2"
          style={{
            backgroundColor: `${headerColor}1f`,
            borderBottomColor: `${headerColor}40`,
          }}
        >
          <Icon className="h-4 w-4 shrink-0" style={{ color: headerColor }} aria-hidden />
          {/* Title stack — node NAME (primary, truncated) with the node_id as a
              small, dimmed subtitle directly beneath it so the canonical id is
              always visible without opening the Inspector. The id is white at
              reduced opacity so it reads as a quiet secondary line against the
              per-type tinted header. */}
          <span className="flex min-w-0 flex-1 flex-col" title={title}>
            <span className="truncate text-[13px] font-semibold leading-5">
              {title}
            </span>
            {(payload.node_id ?? id) && (
              <span
                data-node-id
                className="truncate text-xs font-normal leading-4 text-content-tertiary"
              >
                {payload.node_id ?? id}
              </span>
            )}
          </span>
          {/* Always-visible execution indicator. Details remain in the
              (spinner/check/✕). Its DETAIL (the error message) now lives in the
              hover card, not a per-icon tooltip. */}
          {execState === 'running' && (
            <span
              data-exec-indicator="running"
              role="img"
              aria-label={t('canvas.exec.running', 'Running…')}
              className="shrink-0 text-state-running"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            </span>
          )}
          {execState === 'completed' && (
            <span
              data-exec-indicator="completed"
              role="img"
              aria-label={t('canvas.exec.completed', 'Completed')}
              className="shrink-0 text-state-success"
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
            </span>
          )}
          {execState === 'error' && (
            <span
              data-exec-indicator="error"
              data-exec-error={execError ?? ''}
              role="img"
              aria-label={t('canvas.exec.error', 'Failed')}
              className="shrink-0 text-state-danger"
            >
              <XCircle className="h-3.5 w-3.5" />
            </span>
          )}
          {/* Always-visible ⚠ marker so problems show WITHOUT hovering; the
              warning DETAIL lines are folded into the hover card. */}
          {warnings.length > 0 && (
            <span
              data-node-warning
              role="img"
              aria-label={t('canvas.warn.badge', { count: warnings.length })}
              className="shrink-0 text-state-warning"
            >
              <AlertTriangle className="h-3.5 w-3.5" />
            </span>
          )}
        </div>

        {/* Body — a COMPACT in/out summary stacked VERTICALLY (one short row
            per line) so nothing clips at the fixed card width. The type label
            sits at the top as a subtle subtitle; each row is
            `flex justify-between` so the label truncates (min-w-0) and the count
            pill stays visible. Height is ~CONSTANT (2 short rows) regardless of
            the field count — detail lives in the hover card + the Inspector. */}
        <div className="flex flex-col gap-1 px-2.5 py-2">
          <span className="truncate text-xs font-medium text-content-secondary" title={label}>
            {label}
          </span>
          <div
            data-node-io="inputs"
            className="flex items-center justify-between gap-2 text-xs text-muted-foreground"
            title={`${t('node_inputs', 'Inputs')}: ${inputCount}`}
          >
            <span className="min-w-0 truncate">{t('node_inputs', 'Inputs')}</span>
            <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-xs font-medium tabular-nums text-foreground/70">
              {inputCount}
            </span>
          </div>
          <div
            data-node-io="outputs"
            className="flex items-center justify-between gap-2 text-xs text-muted-foreground"
            title={`${t('node_outputs', 'Outputs')}: ${outputCount}`}
          >
            <span className="min-w-0 truncate">{t('node_outputs', 'Outputs')}</span>
            <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-xs font-medium tabular-nums text-foreground/70">
              {outputCount}
            </span>
          </div>
        </div>

        {!isEnd && (
          <Handle type="source" position={Position.Right} className={HANDLE_CLASS} />
        )}

        {/* Loop-back bottom handles — dedicated, user-inert (isConnectable
            false) attach points for the config-derived loop-back edge that runs
            LoopEnd-bottom → LoopBegin-bottom (see pairingEdgeFor). Rendered ONLY
            on Loop boundaries to avoid clutter elsewhere. */}
        {isLoopEnd && (
          <Handle
            type="source"
            id={LOOP_BACK_SOURCE_HANDLE_ID}
            position={Position.Bottom}
            isConnectable={false}
            className={LOOP_BACK_HANDLE_CLASS}
          />
        )}
        {isLoopBegin && (
          <Handle
            type="target"
            id={LOOP_BACK_TARGET_HANDLE_ID}
            position={Position.Bottom}
            isConnectable={false}
            className={LOOP_BACK_HANDLE_CLASS}
          />
        )}
      </div>
    </NodeHoverCard>
      <NodeOutputPreview
        nodeId={id}
        nodeType={nodeType}
        format={typeof payload.node_config?.output_format === 'string' ? payload.node_config.output_format : undefined}
      />
    </div>
  );
}

export const CustomNode = memo(CustomNodeImpl);
