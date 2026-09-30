/**
 * Per-node-type naming + config defaults — the UI's guard rail against the
 * engine's STRICT naming constraints (verified against
 * `engine/src/vibecanvas_engine/nodes/{start,end,code}.py`):
 *
 *   - StartNode.check  → `node_name` MUST be the const `"__start__"`
 *     (start.py:128-130). A hand-built StartNode with any other name fails
 *     `Workflow.check` with a terse jsonschema `const` error.
 *   - EndNode.check    → `node_name` MUST be the const `"__end__"`
 *     (end.py:94-96).
 *   - CodeNode runtime → the sandbox extracts the symbol `process_fn` by name
 *     and raises `ValueError("The provided code must explicitly define a
 *     function named 'process_fn'.")` (sandbox.py:393-394) if it's missing.
 *     CodeNode.CONFIG_SCHEMA also `required: [programming_language, process_fn]`
 *     (code.py:30-33), so an empty config fails Check too.
 *
 * `RESERVED_NODE_NAMES` lists the node types whose `node_name` is a fixed,
 * engine-reserved constant the user must NOT edit (the inspector renders the
 * name field read-only for these — single Start/End per workflow means no
 * collision risk).
 *
 * `applyNodeTypeDefaults(payload)` is called inside the store's add-node seam
 * so every insertion path (Nodes palette, template drag/insert, onboarding
 * seed) gets canonical names + a runnable CodeNode skeleton for free.
 */

/** Node types whose `node_name` is an engine-reserved constant (read-only). */
export const RESERVED_NODE_NAMES: Readonly<Record<string, string>> = {
  StartNode: '__start__',
  EndNode: '__end__',
};

/**
 * Working CodeNode skeleton: defines a function literally named `process_fn`
 * taking a single `inputs` dict and returning a dict — exactly the symbol the
 * sandbox extracts. Pre-seeding this means a freshly-added CodeNode is
 * runnable and correctly named without the user knowing the contract.
 */
export const CODE_NODE_PROCESS_FN_SKELETON =
  'def process_fn(inputs):\n    # your code here\n    return inputs';

type NodeRec = Record<string, unknown>;

/**
 * Returns the engine-reserved `node_name` for a type, or `undefined` if the
 * type lets the user name the node freely.
 */
export function reservedNodeName(nodeType: string | undefined): string | undefined {
  if (nodeType === undefined) return undefined;
  return RESERVED_NODE_NAMES[nodeType];
}

/** True iff this node type's name is engine-reserved (→ read-only in the inspector). */
export function hasReservedNodeName(nodeType: string | undefined): boolean {
  return reservedNodeName(nodeType) !== undefined;
}

/**
 * Mutate a freshly-built node payload IN PLACE to satisfy the engine's strict
 * naming/shape constraints for its type:
 *   - StartNode / EndNode → stamp the reserved `node_name`.
 *   - CodeNode → seed a runnable `process_fn` skeleton + `programming_language`
 *     when `node_config` doesn't already supply them (templates keep theirs).
 *
 * Returns the same object for convenience. Safe to call on any payload — types
 * without a rule are returned unchanged.
 */
export function applyNodeTypeDefaults(node: NodeRec): NodeRec {
  const nodeType = node.node_type as string | undefined;

  const reserved = reservedNodeName(nodeType);
  if (reserved !== undefined) {
    node.node_name = reserved;
  }

  if (nodeType === 'CodeNode') {
    const cfg =
      node.node_config && typeof node.node_config === 'object'
        ? (node.node_config as NodeRec)
        : {};
    if (typeof cfg.programming_language !== 'string') {
      cfg.programming_language = 'python';
    }
    if (typeof cfg.process_fn !== 'string' || cfg.process_fn.trim() === '') {
      cfg.process_fn = CODE_NODE_PROCESS_FN_SKELETON;
    }
    node.node_config = cfg;
  }

  if (nodeType === 'SubAgentNode') {
    node.output_fields = {
      ...(node.output_fields as NodeRec ?? {}),
      __traces__: { type: 'array', description: 'Runtime execution messages (ChatML)' },
    };
    const hasConfig =
      node.node_config &&
      typeof node.node_config === 'object' &&
      !Array.isArray(node.node_config) &&
      Object.keys(node.node_config as NodeRec).length > 0;
    if (!hasConfig) {
      node.node_config = {
        task_template: '',
        model_name: '',
        max_iterations: 25,
      };
    }
  }

  return node;
}
