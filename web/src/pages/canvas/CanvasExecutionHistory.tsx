import { useMemo, type ReactNode } from 'react';
import { useWorkflowExecutionStatus } from '@/lib/api/queries/executions';
import { useExecutionDetail, useExecutionEvents, type ExecutionFrame } from '@/lib/api/queries/workflow-history';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useNodeExecStore } from '@/stores/node-exec';
import { ExecutionHistoryContext } from './ExecutionHistoryContext';

/** Restore live approvals from durable state, independently of the SSE viewer. */
export function CanvasExecutionHistory({ wfId, enabled, children }: { wfId: string; enabled: boolean; children: ReactNode }) {
  const running = useExecStreamStore((s) => s.wfId === wfId && s.status === 'running');
  const nodeRunning = useNodeExecStore((s) => s.wfId === wfId && s.status === 'running');
  const current = useWorkflowExecutionStatus(wfId, { enabled, running: running || nodeRunning });
  const id = enabled ? current.data?.history_id ?? '' : '';
  const detail = useExecutionDetail(id).data;
  const events = useExecutionEvents(detail ? id : '').data;
  const value = useMemo(() => {
    if (!detail || detail.wf_id !== wfId) return null;
    const latestNodeEvents: Record<string, ExecutionFrame> = {};
    for (const event of events?.events ?? []) {
      if (event.type === 'node_event' && event.node_id) latestNodeEvents[event.node_id] = event;
    }
    return { detail, latestNodeEvents };
  }, [detail, events, wfId]);
  return <ExecutionHistoryContext.Provider value={value}>{children}</ExecutionHistoryContext.Provider>;
}
