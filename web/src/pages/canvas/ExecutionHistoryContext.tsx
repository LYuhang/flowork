import { createContext, useContext } from 'react';
import type { ExecutionDetail, ExecutionFrame } from '@/lib/api/queries/workflow-history';

export interface ExecutionHistoryValue {
  detail: ExecutionDetail;
  latestNodeEvents: Record<string, ExecutionFrame>;
}

/** Execution state is scoped to this graph, never to the editable canvas store. */
export const ExecutionHistoryContext = createContext<ExecutionHistoryValue | null>(null);
export const useExecutionHistory = () => useContext(ExecutionHistoryContext);

/** A refreshed page has no local SSE owner, but the execution is still live. */
export function useActiveHistoryExecution(wfId: string) {
  const history = useExecutionHistory();
  const detail = history?.detail;
  return detail?.wf_id === wfId && ['queued', 'running', 'waiting_approval'].includes(detail.status)
    ? detail : null;
}
