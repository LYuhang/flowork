/**
 * Shared types for the per-type config editors.
 *
 * Every editor takes the same shape so the dispatcher in
 * `NodeConfigEditor.tsx` can treat them uniformly:
 *
 *   - `config` — the current `node_config` object for the selected node.
 *     Always a `Record<string, unknown>`; editors narrow individual keys.
 *   - `readOnly` — disables every input; used for read-only / view-only
 *     mode (e.g. version pinning, T14).
 *   - `onChange` — replaces the entire `node_config` with the new dict.
 *     Editors should treat `config` as *immutable* and emit a new object.
 */
export interface NodeConfigEditorProps {
  config: Record<string, unknown>;
  workflowSettings?: { timeouts?: { http?: number; code?: number } };
  readOnly?: boolean;
  onChange: (next: Record<string, unknown>) => void;
  /**
   * The selected node's OUTPUT field names. Only consumed by editors that
   * cross-check the config against declared outputs (e.g. PromptNode's
   * "is each output field referenced in the template?" hint). Optional so
   * existing editors / tests that don't pass it keep working.
   */
  outputFieldNames?: string[];
  /**
   * The selected node's INPUT field names. Consumed by editors that map
   * outputs back to a chosen input (e.g. TransformNode's per-output mapping
   * blocks). Optional so existing editors / tests that don't pass it keep
   * working.
   */
  inputFieldNames?: string[];
  /**
   * The selected node's INPUT fields as a typed map (`{ name: { type } }`).
   * Consumed by editors that filter inputs by type (e.g. TableWrite's
   * `data_write` dropdown, which only offers object/array fields). Optional so
   * existing editors / tests that don't pass it keep working.
   */
  inputFields?: Record<string, { type?: string }>;
  /**
   * The selected node's id + its workflow id. Only consumed by editors that
   * surface cross-version history (currently PromptNode's prompt_template
   * "History" diff modal). Optional so existing editors / tests that don't
   * pass them keep working.
   */
  nodeId?: string;
  wfId?: string;
}
