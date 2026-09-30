/**
 * Naming-guidance defaults — verified against the engine's strict constraints:
 *   - StartNode.check  → node_name const '__start__' (start.py:128).
 *   - EndNode.check    → node_name const '__end__'   (end.py:94).
 *   - CodeNode runtime → must define `process_fn` (sandbox.py:393) and
 *     config requires programming_language + process_fn (code.py:30).
 */
import { describe, it, expect } from 'vitest';
import {
  applyNodeTypeDefaults,
  reservedNodeName,
  hasReservedNodeName,
  CODE_NODE_PROCESS_FN_SKELETON,
} from '@/lib/workflow/node-type-defaults';

describe('reservedNodeName / hasReservedNodeName', () => {
  it('maps Start/End to their reserved names', () => {
    expect(reservedNodeName('StartNode')).toBe('__start__');
    expect(reservedNodeName('EndNode')).toBe('__end__');
    expect(hasReservedNodeName('StartNode')).toBe(true);
    expect(hasReservedNodeName('EndNode')).toBe(true);
  });

  it('returns undefined / false for free-named types', () => {
    expect(reservedNodeName('CodeNode')).toBeUndefined();
    expect(reservedNodeName(undefined)).toBeUndefined();
    expect(hasReservedNodeName('PromptNode')).toBe(false);
  });
});

describe('applyNodeTypeDefaults', () => {
  it('stamps __start__ on a StartNode (overriding any provided name)', () => {
    const n = applyNodeTypeDefaults({ node_type: 'StartNode', node_name: 'whatever' });
    expect(n.node_name).toBe('__start__');
  });

  it('stamps __end__ on an EndNode', () => {
    const n = applyNodeTypeDefaults({ node_type: 'EndNode' });
    expect(n.node_name).toBe('__end__');
  });

  it('seeds a runnable process_fn skeleton + python on a fresh CodeNode', () => {
    const n = applyNodeTypeDefaults({ node_type: 'CodeNode', node_config: {} });
    const cfg = n.node_config as Record<string, unknown>;
    expect(cfg.programming_language).toBe('python');
    expect(cfg.process_fn).toBe(CODE_NODE_PROCESS_FN_SKELETON);
    // The skeleton names the function exactly `process_fn(inputs)`.
    expect(cfg.process_fn).toContain('def process_fn(inputs):');
  });

  it('does not clobber an existing CodeNode process_fn (template case)', () => {
    const existing = 'def process_fn(inputs):\n    return {"x": 1}';
    const n = applyNodeTypeDefaults({
      node_type: 'CodeNode',
      node_config: { programming_language: 'python', process_fn: existing },
    });
    expect((n.node_config as Record<string, unknown>).process_fn).toBe(existing);
  });

  it('replaces a blank/whitespace CodeNode process_fn with the skeleton', () => {
    const n = applyNodeTypeDefaults({
      node_type: 'CodeNode',
      node_config: { process_fn: '   ' },
    });
    expect((n.node_config as Record<string, unknown>).process_fn).toBe(
      CODE_NODE_PROCESS_FN_SKELETON,
    );
  });

  it('leaves a free-named node (PromptNode) untouched', () => {
    const n = applyNodeTypeDefaults({ node_type: 'PromptNode', node_name: 'judge' });
    expect(n.node_name).toBe('judge');
    expect(n.node_config).toBeUndefined();
  });
});

it('adds reserved SubAgent traces without losing business outputs', () => {
  const node = applyNodeTypeDefaults({
    node_type: 'SubAgentNode',
    output_fields: { answer: { type: 'string' } },
  });
  expect(node.output_fields).toEqual({
    answer: { type: 'string' },
    __traces__: { type: 'array', description: 'Runtime execution messages (ChatML)' },
  });
});
