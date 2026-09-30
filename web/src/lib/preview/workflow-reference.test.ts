import { describe, expect, it } from 'vitest';
import { workflowReference } from './workflow-reference';

describe('workflow context references', () => {
  it('pins the viewed version for whole-workflow references', () => {
    const result = workflowReference('wf-1', 'v3.sv2', 'Report');
    expect(result.resource).toEqual({ kind: 'workflow', workflow_id: 'wf-1', version: 'v3.sv2' });
    expect(result.selector).toBeUndefined();
  });
  it('creates one deterministic selection without persisting canvas edge ids or client configuration', () => {
    const edge = { id: 'temporary-render-id', source: 'a', target: 'b', selected: true };
    const result = workflowReference('wf-1', 'v1.sv4', 'Selected objects', ['b', 'a', 'a'], [edge, edge]);
    expect(result.selector).toEqual({ kind: 'workflow_elements', node_ids: ['a', 'b'], edges: [{ source: 'a', target: 'b' }] });
    expect(JSON.stringify(result)).not.toContain('temporary-render-id');
    expect(result.snapshot).toBeUndefined();
  });
});
