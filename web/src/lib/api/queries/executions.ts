import { useQuery } from '@tanstack/react-query';
import { getWorkflowExecutionStatus, checkWorkflowResume } from '@/lib/api/executions';

/** Latest DB-backed interactive execution state for a workflow. */
export const useWorkflowExecutionStatus = (
  wfId: string | undefined,
  opts: { enabled: boolean; running?: boolean },
) =>
  useQuery({
    queryKey: ['executions', 'workflow-status', wfId],
    queryFn: () => getWorkflowExecutionStatus(wfId as string),
    enabled: opts.enabled && !!wfId,
    refetchInterval: (query) =>
      opts.running || query.state.data?.status === 'running' ? 1_000 : false,
    staleTime: 1_000,
  });


export const useWorkflowResumeCheck = (
  wfId: string, workflow: unknown, input: Record<string, unknown>, status: string, enabled: boolean,
) => useQuery({
  queryKey: ['executions', 'resume-check', wfId, workflow, input, status],
  queryFn: ({ signal }) => checkWorkflowResume(wfId, workflow, input, signal),
  enabled: enabled && !!workflow,
  staleTime: 0,
  retry: false,
});
