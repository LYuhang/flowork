import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { resolveApiUrl } from '@/lib/base-path';
import { sessionFetch } from '@/lib/api/session-fetch';
import type { WorkflowDraft } from '@/stores/workflow-edit';

export type ExecutionStatus = 'queued' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'timed_out' | 'cancelled';
export type ExecutionSource = 'workflow' | 'task' | 'deployment';
export interface ExecutionSummary {
  id: string;
  created_at: string;
  status: ExecutionStatus;
  input_index: number | null;
  pending_approvals?: { id: string; node_id: string; approver_email: string | null; deadline: string }[];
}
interface ExecutionHistoryPage { items: ExecutionSummary[]; has_more: boolean; server_time?: string; received_at?: number }
interface HistoryCursor { before_time: string; before_id: string }
export interface ExecutionApproval {
  id: string;
  node_id: string;
  status: 'pending' | 'decision_requested' | 'approved' | 'rejected' | 'timeout' | 'cancelled' | 'execution_lost';
  deadline: string;
  approved: boolean | null;
  can_decide: boolean;
}
export interface ExecutionDetail {
  id: string;
  wf_id: string;
  source_type: 'workflow' | 'task' | 'deployment';
  source_id: string;
  status: ExecutionStatus;
  last_seq: number;
  error_code?: string | null;
  server_time: string;
  received_at: number;
  workflow: WorkflowDraft;
  inputs: Record<string, unknown>;
  approvals: ExecutionApproval[];
  result: Record<string, unknown> | null;
}
export interface ExecutionFrame {
  seq: number;
  type: string;
  node_id?: string;
  status?: string;
  output?: unknown;
  inputs?: unknown;
  error_message?: string;
  execution_time?: number;
  [key: string]: unknown;
}
interface ExecutionEvents {
  events: ExecutionFrame[];
  last_seq: number;
  status: ExecutionStatus;
}

export const executionDetailKey = (id: string) => ['workflow-execution', id] as const;
export const executionActive = (status?: string) => !status || ['queued', 'running', 'waiting_approval'].includes(status);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await sessionFetch(resolveApiUrl(`/api/v1/workflow-executions${path}`), init);
  if (!response.ok) throw new Error(`Execution request failed (${response.status})`);
  return response.json() as Promise<T>;
}

export function useExecutionDetail(id: string) {
  return useQuery({
    queryKey: executionDetailKey(id),
    enabled: Boolean(id),
    queryFn: async () => ({ ...await request<ExecutionDetail>(`/${encodeURIComponent(id)}`), received_at: Date.now() }),
    refetchInterval: (query) => executionActive(query.state.data?.status) ? 1000 : false,
    retry: false,
  });
}

export function useExecutionHistory(source: ExecutionSource, id: string, filter: ExecutionStatus | 'all' | 'mine') {
  return useInfiniteQuery({
    queryKey: ['workflow-execution-history', source, id, filter],
    enabled: Boolean(id),
    initialPageParam: null as HistoryCursor | null,
    queryFn: async ({ pageParam }) => {
      const query = new URLSearchParams({ source_type: source, source_id: id, limit: '25' });
      if (filter === 'mine') query.set('mine', 'true');
      else if (filter !== 'all') query.append('statuses', filter);
      if (pageParam) {
        query.set('before_time', pageParam.before_time);
        query.set('before_id', pageParam.before_id);
      }
      return { ...await request<ExecutionHistoryPage>(`?${query}`), received_at: Date.now() };
    },
    getNextPageParam: (page) => {
      const last = page.items.at(-1);
      return page.has_more && last ? { before_time: last.created_at, before_id: last.id } : undefined;
    },
    refetchInterval: 3000,
    retry: false,
  });
}

export function useExecutionEvents(id: string) {
  const client = useQueryClient();
  const key = ['workflow-execution-events', id] as const;
  return useQuery({
    queryKey: key,
    enabled: Boolean(id),
    queryFn: async () => {
      const previous = client.getQueryData<ExecutionEvents>(key)?.events ?? [];
      const after = previous.at(-1)?.seq ?? 0;
      const next = await request<ExecutionEvents>(`/${encodeURIComponent(id)}/events?after=${after}&limit=500`);
      return { ...next, events: [...previous, ...next.events] };
    },
    refetchInterval: (query) => {
      const data = query.state.data;
      if ((data?.events.at(-1)?.seq ?? 0) < (data?.last_seq ?? 0)) return 100;
      return executionActive(data?.status) ? 1000 : false;
    },
    retry: false,
  });
}

export function useApprovalDecision(executionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ approvalId, approved }: { approvalId: string; approved: boolean }) => request(
      `/${encodeURIComponent(executionId)}/approvals/${encodeURIComponent(approvalId)}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved }) },
    ),
    onSuccess: () => Promise.all([
      client.invalidateQueries({ queryKey: executionDetailKey(executionId) }),
      client.invalidateQueries({ queryKey: ['workflow-execution-history'] }),
    ]),
    onError: () => client.invalidateQueries({ queryKey: executionDetailKey(executionId) }),
  });
}
