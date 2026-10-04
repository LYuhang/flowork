import { resolveApiUrl } from '@/lib/base-path';
import { sessionFetch } from '@/lib/api/session-fetch';

export type InstanceWorkflowSource = { type: 'task' | 'deployment' | 'execution'; id: string };

export async function loadInstanceWorkflow(source: InstanceWorkflowSource, workflowId: string, version: string) {
  const query = new URLSearchParams({ workflow_id: workflowId, version });
  const path = source.type === 'execution'
    ? `/api/v1/workflow-executions/${encodeURIComponent(source.id)}`
    : `/api/v1/${source.type === 'task' ? 'tasks' : 'deployments'}/${encodeURIComponent(source.id)}/workflow-preview?${query}`;
  const response = await sessionFetch(resolveApiUrl(path), { credentials: 'include' });
  if (!response.ok) throw new Error('Workflow snapshot is unavailable.');
  const data = await response.json();
  if (source.type === 'execution' && (data.wf_id !== workflowId || data.workflow_version !== version)) {
    throw new Error('Execution workflow version does not match.');
  }
  return data as { workflow: Record<string, unknown>; meta?: { workflow_name?: string } };
}
