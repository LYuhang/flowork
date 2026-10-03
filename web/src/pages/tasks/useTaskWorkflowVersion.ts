import { useState } from 'react';
import { useWorkflow, useWorkflowAt, useWorkflowVersions } from '@/lib/api/queries/workflow';

/** Keep version selection local to the form, never in Chat or global Workflow state. */
export function useTaskWorkflowVersion(workflowId: string, pinnedVersion?: string) {
  const live = useWorkflow(workflowId);
  const history = useWorkflowVersions(workflowId);
  const [choice, setChoice] = useState({ workflowId: '', value: '' });
  const metadata = live.data?.workflow?.__meta__ as { workflow_version?: number; workflow_subversion?: number } | undefined;
  const currentMajor = live.data?.meta?.active_v ?? metadata?.workflow_version;
  const currentSub = live.data?.meta?.active_sv ?? metadata?.workflow_subversion;
  const selector = pinnedVersion || (choice.workflowId === workflowId && choice.value
    ? choice.value : currentMajor && currentSub != null ? `v${currentMajor}.sv${currentSub}` : '');
  const rows = (history.data as { versions?: { major: number; sub: number }[] } | undefined)?.versions ?? [];
  const match = /^v([1-9]\d*)(?:\.sv(\d+))?$/.exec(selector);
  const major = match ? Number(match[1]) : null;
  const availableSubs = rows.filter(row => row.major === major).map(row => row.sub);
  const sub = match?.[2] !== undefined ? Number(match[2])
    : availableSubs.length ? Math.max(...availableSubs) : major === currentMajor ? currentSub ?? null : null;
  const isLive = major === currentMajor && sub === currentSub;
  const pinned = useWorkflowAt(workflowId, isLive ? null : major, isLive ? null : sub);
  const choices = [...new Set([
    ...(currentMajor && currentSub != null ? [`v${currentMajor}.sv${currentSub}`] : []),
    ...rows.map(row => `v${row.major}.sv${row.sub}`),
  ])];
  return {
    selector, choices,
    select: (value: string) => setChoice({ workflowId, value }),
    snapshot: isLive || !selector ? live : pinned,
    loading: live.isLoading || history.isLoading,
    error: live.isError || history.isError,
    target: { version: selector },
    frozenTarget: major !== null && sub !== null ? { version: `v${major}.sv${sub}` } : undefined,
  };
}
