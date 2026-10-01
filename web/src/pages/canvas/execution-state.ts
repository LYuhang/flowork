import type { ExecutionStatus } from '@/lib/api/queries/workflow-history';

/** A terminal run cannot still have a live node; preserve its recorded successes. */
export function historicalNodeStatus(status: string | undefined, runStatus: ExecutionStatus) {
  if (status !== 'running') return status;
  if (runStatus === 'failed' || runStatus === 'timed_out') return 'error';
  if (runStatus === 'cancelled' || runStatus === 'succeeded') return 'cancelled';
  return status;
}
