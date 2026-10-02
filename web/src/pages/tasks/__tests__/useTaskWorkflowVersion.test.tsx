import { renderHook, act } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useTaskWorkflowVersion } from '../useTaskWorkflowVersion';

vi.mock('@/lib/api/queries/workflow', () => ({
  useWorkflow: () => ({ data: { meta: { active_v: 2, active_sv: 3 },
    workflow: { __meta__: { workflow_version: 1, workflow_subversion: 0 } } } }),
  useWorkflowVersions: () => ({ data: { versions: [{ major: 1, sub: 0 }, { major: 2, sub: 3 }] } }),
  useWorkflowAt: (_id: string, v: number, sv: number) => ({ data: { workflow: {}, version: `v${v}.sv${sv}` } }),
}));

it('uses authoritative saved metadata and offers only complete fixed versions', () => {
  const { result } = renderHook(() => useTaskWorkflowVersion('wf'));
  expect(result.current.selector).toBe('v2.sv3');
  expect(result.current.target).toEqual({ version: 'v2.sv3' });
  expect(result.current.choices).toEqual(['v2.sv3', 'v1.sv0']);
  act(() => result.current.select('v1.sv0'));
  expect(result.current.target).toEqual({ version: 'v1.sv0' });
  expect(result.current.frozenTarget).toEqual({ version: 'v1.sv0' });
});
