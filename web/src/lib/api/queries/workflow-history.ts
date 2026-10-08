import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { refreshResourceQuery, watchResourceActivity } from '@/lib/api/sse/resource-activity';
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
interface HistoryWindow { pages: ExecutionHistoryPage[]; boundary: HistoryCursor | null }
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

class ExecutionRequestError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(`Execution request failed (${status})`);
    this.status = status;
  }
}
const accessUnavailable = (error: unknown) => error instanceof ExecutionRequestError
  && [401, 403, 404].includes(error.status);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await sessionFetch(resolveApiUrl(`/api/v1/workflow-executions${path}`), init);
  if (!response.ok) throw new ExecutionRequestError(response.status);
  return response.json() as Promise<T>;
}

export function useExecutionDetail(id: string) {
  const client = useQueryClient();
  useEffect(() => {
    if (!id) return;
    return watchResourceActivity(`/api/v1/workflow-executions/${encodeURIComponent(id)}/activity`, async () => {
      await Promise.all([
        refreshResourceQuery(client, executionDetailKey(id)),
        refreshResourceQuery(client, ['workflow-execution-events', id]),
      ]);
    });
  }, [client, id]);
  return useQuery({
    queryKey: executionDetailKey(id),
    enabled: Boolean(id),
    queryFn: async () => ({ ...await request<ExecutionDetail>(`/${encodeURIComponent(id)}`), received_at: Date.now() }),
    retry: false,
  });
}

const historyCursor = (item: ExecutionSummary): HistoryCursor => ({ before_time: item.created_at, before_id: item.id });
const atOrAbove = (item: ExecutionSummary, boundary: HistoryCursor) => {
  // PostgreSQL cursor timestamps retain microseconds; Date alone truncates
  // them and can misorder records created within the same millisecond.
  const micros = (value: string) => Date.parse(value) * 1000
    + Number((value.match(/\.(\d+)/)?.[1] ?? '').padEnd(6, '0').slice(3, 6));
  const difference = micros(item.created_at) - micros(boundary.before_time);
  return difference > 0 || (difference === 0 && item.id >= boundary.before_id);
};

export function useExecutionHistory(source: ExecutionSource, id: string, filter: ExecutionStatus | 'all' | 'mine') {
  const client = useQueryClient();
  const key = ['workflow-execution-history', source, id, filter];
  const readPage = async (before: HistoryCursor | null, limit: number, signal?: AbortSignal) => {
    const params = new URLSearchParams({ source_type: source, source_id: id, limit: String(limit) });
    if (filter === 'mine') params.set('mine', 'true');
    else if (filter !== 'all') params.append('statuses', filter);
    if (before) {
      params.set('before_time', before.before_time);
      params.set('before_id', before.before_id);
    }
    try {
      return { ...await request<ExecutionHistoryPage>(`?${params}`, { signal }), received_at: Date.now() };
    } catch (error) {
      // Do not redisplay a revoked snapshot if a later request fails offline.
      if (accessUnavailable(error)) client.setQueryData<HistoryWindow>(key, { pages: [], boundary: null });
      throw error;
    }
  };
  const next = useMutation({
    mutationFn: async (target: { key: string[]; readPage: typeof readPage }) => {
      await client.cancelQueries({ queryKey: target.key, exact: true });
      const previous = client.getQueryData<HistoryWindow>(target.key);
      if (!previous?.pages.at(-1)?.has_more) return previous;
      const page = await target.readPage(previous.boundary, 25);
      const last = page.items.at(-1);
      return { pages: [...previous.pages, page], boundary: last ? historyCursor(last) : previous.boundary };
    },
    onSuccess: async (data, target) => {
      await client.cancelQueries({ queryKey: target.key, exact: true });
      if (data) client.setQueryData(target.key, data);
    },
  });
  useEffect(() => { next.reset(); }, [source, id, filter, next.reset]);
  const query = useQuery({
    queryKey: key,
    enabled: Boolean(id) && !next.isPending,
    queryFn: async ({ signal }): Promise<HistoryWindow> => {
      const previous = client.getQueryData<HistoryWindow>(key);
      const boundary = previous?.boundary ?? null;
      // Refresh the entire loaded time range, including old records newly
      // matching a status/assignee filter. Larger batches avoid one HTTP and
      // authorization round trip for each previously loaded 25-row page.
      const pages: ExecutionHistoryPage[] = [];
      let cursor: HistoryCursor | null = null;
      do {
        const page = await readPage(cursor, boundary ? 1000 : 25, signal);
        const last = page.items.at(-1);
        const within = boundary ? page.items.filter(item => atOrAbove(item, boundary)) : page.items;
        pages.push({ ...page, items: within, has_more: page.has_more || within.length < page.items.length });
        if (!boundary) return { pages, boundary: last ? historyCursor(last) : null };
        if (!last || !page.has_more || !atOrAbove(last, boundary) || last.id === boundary.before_id) break;
        cursor = historyCursor(last);
      } while (!signal.aborted);
      return { pages, boundary };
    },
    staleTime: 3000,
    refetchInterval: current => accessUnavailable(current.state.error) || accessUnavailable(next.error) ? false : 3000,
    retry: false,
  });
  return {
    ...query,
    isError: query.isError || next.isError,
    refetch: () => { next.reset(); return query.refetch(); },
    isFetching: query.isFetching || next.isPending,
    hasNextPage: Boolean(query.data?.pages.at(-1)?.has_more),
    isFetchingNextPage: query.isFetching || next.isPending,
    fetchNextPage: () => next.mutateAsync({ key, readPage }).catch(() => undefined),
  };
}

export function useExecutionEvents(id: string) {
  const client = useQueryClient();
  const key = ['workflow-execution-events', id] as const;
  return useQuery({
    queryKey: key,
    enabled: Boolean(id),
    queryFn: async ({ signal }) => {
      const previous = client.getQueryData<ExecutionEvents>(key)?.events ?? [];
      return readExecutionEvents(id, previous, signal);
    },
    retry: false,
  });
}

export async function readExecutionEvents(id: string, previous: ExecutionFrame[], signal?: AbortSignal): Promise<ExecutionEvents> {
  const frames = [...previous];
  let cursor = frames.at(-1)?.seq ?? 0;
  let target: number | undefined;
  let next: ExecutionEvents;
  do {
    next = await request<ExecutionEvents>(`/${encodeURIComponent(id)}/events?after=${cursor}&limit=500`, { signal });
    // Catch up to the head observed at the start, rather than chasing a
    // continuously growing producer forever. Later invalidations catch the tail.
    target ??= next.last_seq;
    const before = cursor;
    for (const frame of next.events) {
      if (frame.seq <= cursor) continue;
      if (frame.seq !== cursor + 1) throw new Error('Execution event sequence is incomplete');
      frames.push(frame);
      cursor = frame.seq;
    }
    if (cursor === before && cursor < target) throw new Error('Execution events are missing');
  } while (cursor < target);
  return { ...next, events: frames };
}

export function useApprovalDecision(executionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ approvalId, approved }: { approvalId: string; approved: boolean }) => request(
      `/${encodeURIComponent(executionId)}/approvals/${encodeURIComponent(approvalId)}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved }) },
    ),
    onSuccess: () => Promise.all([
      refreshResourceQuery(client, executionDetailKey(executionId)),
      client.invalidateQueries({ queryKey: ['workflow-execution-history'] }),
    ]),
    onError: () => refreshResourceQuery(client, executionDetailKey(executionId)),
  });
}
