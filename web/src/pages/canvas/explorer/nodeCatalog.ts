/**
 * Node-type catalog for the Explorer "Nodes" palette (#11).
 *
 * The canonical node-type list + display labels are sourced from
 * `NODE_TYPES.ts` (`NODE_LABELS`). We do not introduce a second hardcoded list. Each
 * entry pairs a node_type with an i18n description KEY (`nodes_palette.desc.*`)
 * so the one-line blurb is translatable; the card resolves it via `t()` with
 * an English fallback that lives only in en.json.
 *
 * The insertable payload is shared by every Nodes-palette insertion path
 * (sans id/name — `useWorkflowEditStore.addNode` allocates the id). Keeping it
 * here means the drag-MIME payload and the click-insert payload are identical.
 */
import { ADDABLE_NODE_TYPES } from '@/pages/canvas/nodes/NODE_TYPES';
import { fixedOutputFields } from '@/pages/canvas/nodes/fixedOutputs';

/** Stable display order for the palette (canonical authoring flow, minus
 *  any deprecated/hidden types in HIDDEN_NODE_TYPES). */
export const NODE_CATALOG: readonly string[] = ADDABLE_NODE_TYPES;

/** i18n key for a node type's one-line description. */
export function nodeDescKey(nodeType: string): string {
  return `nodes_palette.desc.${nodeType}`;
}

/**
 * The canonical insertable skeleton for the Nodes palette. `addNode` fills
 * node_id/node_name; the drag seam serializes this object to
 * the `application/vibecanvas-node` MIME.
 */
export function nodeInsertPayload(nodeType: string): Record<string, unknown> {
  return {
    node_type: nodeType,
    node_description: '',
    input_fields: {},
    output_fields: nodeType === 'HumanApprovalNode' ? structuredClone(fixedOutputFields(nodeType)) : {},
    node_config: nodeType === 'HumanApprovalNode'
      ? { instruction: '', approver_email: '', timeout_seconds: 3600 }
      : {},
    children: [],
  };
}
