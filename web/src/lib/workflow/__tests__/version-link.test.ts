import { describe, expect, it } from 'vitest';
import { workflowVersionHref, deploymentWorkflowVersion } from '../version-link';
import type { Deployment } from '@/lib/api/deployments';

describe('exact workflow version links', () => {
  it.each(['head', 'v2', '', 'v0.sv1', 'v2.sv-1', undefined, null])('does not navigate an ambiguous version %s', version => {
    expect(workflowVersionHref('wf', version)).toBeNull();
  });
  it('does not substitute configured or latest versions for an unavailable serving revision', () => {
    const dep = { active_revision_id: 'old', version_pin: 'specific', pinned_major: 9, pinned_sub: 3,
      runtime: { instances: [{ id: 'next', version: 'v9.sv3', state: 'preparing' }] } } as Deployment;
    expect(deploymentWorkflowVersion(dep)).toEqual({ serving: true, version: undefined });
    expect(deploymentWorkflowVersion({ ...dep, active_revision_id: null })).toEqual({ serving: false, version: 'v9.sv3' });
  });
});
