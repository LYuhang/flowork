/**
 * Executions REST client (read side) — hand-rolled like `vfs.ts`.
 *
 * The SSE *producer* (`sse/exec-stream.ts`) feeds the live exec-stream store
 * while a run is in flight. This module covers the complementary read path:
 * `GET /api/v1/workflows/{wf_id}/execution/status` returns the latest
 * durable state, or JSON `null` when the workflow has not run yet.
 *
 * The route returns `ExecutionStatusOut` whose `result` field is the process-local
 * `per_node` map, keyed by node_id with the shape
 * `{status, execution_result, error, inputs}`. Note the status field is
 * `execution_result` (the engine's name) whereas the live exec-stream store
 * uses `result`; the hydration consumer maps between them.
 */
import { getApiBase } from '@/lib/base-path';

const BASE = getApiBase();

/** A single persisted per-node row inside `ExecutionStatusOut.result`. */
export interface ExecutionPerNodeRow {
  status?: string;
  execution_result?: string;
  error?: string;
  inputs?: unknown;
  /** Per-node wall-clock seconds, persisted by the accumulator so a reloaded
   * run can show the node's duration too (the live path reads it off the SSE
   * frame). Absent for rows recorded before timing was wired. */
  duration?: number;
}

export interface ExecutionStatusOut {
  exec_id: string;
  history_id?: string | null;
  wf_id: string;
  status: 'running' | 'completed' | 'stopped' | 'error';
  started_at: number;
  finished_at?: number | null;
  /** Persisted per-node map (`per_node`). Null until the first node boundary. */
  result?: Record<string, ExecutionPerNodeRow> | null;
  error?: string | null;
}

async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { useAuthStore } = await import('@/stores/auth');
  const token = useAuthStore.getState().token;
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (resp.status === 401) useAuthStore.getState().handle401();
  return resp;
}

export async function getWorkflowExecutionStatus(
  wfId: string,
): Promise<ExecutionStatusOut | null> {
  const resp = await authedFetch(
    `/api/v1/workflows/${encodeURIComponent(wfId)}/execution/status`,
  );
  if (!resp.ok) {
    throw new Error(
      `getWorkflowExecutionStatus failed: ${resp.status} ${resp.statusText}`,
    );
  }
  return (await resp.json()) as ExecutionStatusOut | null;
}

export async function cancelWorkflowExecution(wfId: string): Promise<void> {
  const resp = await authedFetch(
    `/api/v1/workflows/${encodeURIComponent(wfId)}/execution/cancel`,
    { method: 'POST' },
  );
  if (resp.status === 404) return;
  if (!resp.ok) {
    throw new Error(
      `cancelWorkflowExecution failed: ${resp.status} ${resp.statusText}`,
    );
  }
}

export async function cancelExecution(execId: string): Promise<void> {
  const resp = await authedFetch(
    `/api/v1/executions/${encodeURIComponent(execId)}/cancel`,
    { method: 'POST' },
  );
  if (!resp.ok) {
    throw new Error(
      `cancelExecution failed: ${resp.status} ${resp.statusText}`,
    );
  }
}


export interface WorkflowResumeCheck {
  previous_inputs: Record<string, unknown> | null;
  eligible: boolean;
  resume_from: string | null;
  reason: 'no_failure' | 'workflow_changed' | 'inputs_changed' | null;
}

export async function checkWorkflowResume(
  wfId: string, workflow: unknown, input: Record<string, unknown>, signal?: AbortSignal,
): Promise<WorkflowResumeCheck> {
  const resp = await authedFetch(
    `/api/v1/workflows/${encodeURIComponent(wfId)}/execution/resume-check`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workflow, input }), signal },
  );
  if (!resp.ok) throw new Error(`checkWorkflowResume failed: ${resp.status}`);
  return await resp.json() as WorkflowResumeCheck;
}
