/**
 * Per-node-type registry of ENGINE-FIXED output fields.
 *
 * Some node types have an output schema the engine enforces EXACTLY — the
 * user neither authors nor edits those fields; the engine always produces
 * them (and `Node.check` rejects a workflow whose `output_fields` don't match
 * the fixed set). For those types the inspector should PRESET the fields +
 * render them READ-ONLY (greyed) so the user sees the node's output format
 * but cannot add/remove/rename/retype them.
 *
 * This complements `outputsFollowInputs` (StartNode/EndNode), where the output
 * is a projection of the *inputs*. Here the output is a CONSTANT, independent
 * of the inputs.
 *
 * The names + types below are copied verbatim from each engine node's `check`
 * / `AGENT_SPEC` (see `engine/src/vibecanvas_engine/nodes/`). Keep them in
 * lockstep with the engine — a drift means a green inspector that fails the
 * route Check.
 *
 *   HTTPRequestNode  → response_body (object), status_code (integer),
 *                      response_headers (object)        [http_request.py]
 *   LoopBeginNode    → loop_output (array), i (integer) [loop.py]
 *   ConditionNode    → condition (string)               [condition.py]
 *   TemplateNode     → rendered (string), format (string) [template.py]
 *   TableReadNode    → rows (array), headers (array),
 *                      row_count (integer), schema (object) [table_read.py]
 *   TableWriteNode   → file_path (string),
 *                      rows_written (integer)           [table_write.py]
 *
 * NOTE: ParallelStart/End + LoopEnd require output_fields to be EMPTY — those
 * are gated by hiding the output block entirely in NodeTab, not via a preset.
 */
import type { FieldsMap } from '@/pages/canvas/inspector/FieldsEditor';

export const FIXED_OUTPUT_FIELDS: Record<string, FieldsMap> = {
  HumanApprovalNode: {
    approved: { type: 'boolean', description: 'Approval decision; false for rejection or timeout' },
  },
  HTTPRequestNode: {
    response_body: { type: 'object', description: 'API response body' },
    status_code: { type: 'integer', description: 'HTTP status code' },
    response_headers: { type: 'object', description: 'Response headers' },
  },
  LoopBeginNode: {
    loop_output: {
      type: 'array',
      description: 'Collected outputs from each iteration',
    },
    i: { type: 'integer', description: 'Current loop index' },
  },
  ConditionNode: {
    condition: { type: 'string', description: 'Matched condition name' },
  },
  TemplateNode: {
    rendered: { type: 'string', description: 'Rendered template output' },
    format: { type: 'string', description: 'Output format (html/markdown/text)' },
  },
  TableReadNode: {
    rows: { type: 'array', description: 'Data rows' },
    headers: { type: 'array', description: 'Column names' },
    row_count: { type: 'integer', description: 'Number of rows read' },
    schema: { type: 'object', description: 'Inferred JSON Schema' },
  },
  TableWriteNode: {
    file_path: { type: 'string', description: 'Written file path' },
    rows_written: { type: 'integer', description: 'Number of rows written' },
  },
};

/** The engine-fixed output_fields for `nodeType`, or `undefined`. */
export function fixedOutputFields(
  nodeType: string | undefined,
): FieldsMap | undefined {
  if (nodeType === undefined) return undefined;
  return FIXED_OUTPUT_FIELDS[nodeType];
}

export function hasFixedOutputs(nodeType: string | undefined): boolean {
  return fixedOutputFields(nodeType) !== undefined;
}

/**
 * Whether `current` already equals the fixed `preset` (same names, types, and
 * descriptions, in the same order). Used to decide whether a draft backfill is
 * needed — a faithful match means no write, keeping the undo stack clean.
 */
export function outputsMatchFixed(
  current: FieldsMap | undefined,
  preset: FieldsMap,
): boolean {
  const cur = current ?? {};
  const curKeys = Object.keys(cur);
  const presetKeys = Object.keys(preset);
  if (curKeys.length !== presetKeys.length) return false;
  for (let idx = 0; idx < presetKeys.length; idx += 1) {
    const key = presetKeys[idx];
    if (curKeys[idx] !== key) return false; // order matters (object-key order)
    const a = cur[key];
    const b = preset[key];
    if (!a || a.type !== b.type || (a.description ?? '') !== (b.description ?? '')) {
      return false;
    }
  }
  return true;
}
