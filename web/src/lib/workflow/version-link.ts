import type { Deployment } from '@/lib/api/deployments';
import type { Task } from '@/lib/api/tasks';

export function workflowVersionHref(workflowId: string | null | undefined, version: unknown): string | null {
  if (!workflowId || typeof version !== 'string' || !/^v[1-9]\d*\.sv\d+$/.test(version)) return null;
  return `/workflow/${encodeURIComponent(workflowId)}/version/${version}`;
}

export function batchWorkflowVersion(task: Task): unknown {
  const payload = task.payload as { workflow_snapshot?: { version?: unknown } } | null;
  // A selector or the current workflow HEAD does not identify a batch's snapshot.
  return payload?.workflow_snapshot?.version;
}

export function deploymentWorkflowVersion(dep: Deployment): { version: unknown; serving: boolean } {
  if (dep.active_revision_id) {
    return { version: dep.runtime?.instances.find(instance => instance.id === dep.active_revision_id)?.version, serving: true };
  }
  // With no serving revision, only a fully pinned configuration is unambiguous.
  return {
    version: dep.version_pin === 'specific' && dep.pinned_major != null && dep.pinned_sub != null
      ? `v${dep.pinned_major}.sv${dep.pinned_sub}` : null,
    serving: false,
  };
}
