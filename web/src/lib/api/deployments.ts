/**
 * Deployments API client (Deployments T14).
 *
 * Thin typed wrappers over the 9 deployment endpoints landed by T4–T13:
 *
 *   * `POST   /api/v1/deployments`                       — create
 *   * `GET    /api/v1/deployments`                       — list (filters)
 *   * `GET    /api/v1/deployments/{id}`                  — single
 *   * `PATCH  /api/v1/deployments/{id}`                  — partial update
 *   * `DELETE /api/v1/deployments/{id}`                  — soft delete
 *   * `POST   /api/v1/deployments/{id}/rotate-key`       — mint new api_key
 *   * `POST   /api/v1/deployments/{id}/test-invoke`      — session-auth run
 *   * `GET    /api/v1/deployments/{id}/metrics`          — bucketed series
 *   * `GET    /api/v1/deployments/{id}/history`          — keyset paged tasks
 *
 * Mirrors the hand-rolled-types approach in `tasks.ts` — these routes were
 * added after the last `pnpm codegen:api:offline` snapshot, so a follow-up
 * regen will eventually tighten them into the typed `apiClient`. Everything
 * funnels through the same `authedFetch` shim, picking up the auth
 * middleware (Bearer header + 401 → reopen login) for free.
 */

import type {
  ResourceAccess,
  ResourceProvenance,
} from '@/lib/api/organizations';

// ---------------------------------------------------------------------------
// Contract — kept in sync with `routes/deployments.py` + the `deployments`
// table CHECK constraints in migration 005.
// ---------------------------------------------------------------------------

export type TriggerType = 'api' | 'webhook';
export type VersionPin = 'head' | 'major' | 'specific';

/** Shape returned by `GET /deployments` (list items) and `GET /{id}`.
 *
 * Secrets (`api_key_hash`, `hmac_secret`) are stripped server-side via
 * `_scrub_secret_fields` — they NEVER appear in read responses. The
 * plaintext `api_key` / `hmac_secret` are returned ONCE at create and
 * (for `api_key`) at rotate-key — that one-shot lives in `CreateResponse`
 * / `RotateKeyResponse` below, not here.
 */
export interface Deployment {
  id: string;
  tenant_id: string;
  user_id: string;
  wf_id: string;
  name: string;
  slug: string;
  trigger_type: TriggerType;
  version_pin: VersionPin;
  pinned_major: number | null;
  pinned_sub: number | null;
  enabled: boolean;
  mount_enabled?: boolean;
  cpu_millis?: number;
  memory_mb?: number;
  rate_limit_qps: number;
  invoke_count: number;
  last_invoked_at: string | null;
  created_at: string;
  updated_at: string | null;
  deleted_at: string | null;
  access: ResourceAccess;
  provenance: ResourceProvenance;
  active_revision_id?: string | null;
  rollout_status?: string;
  rollout_error?: string | null;
  runtime?: { instances: DeploymentInstance[] };
}

export interface DeploymentInstance {
  metrics?: { memory_bytes: number | null; cpu_percent: number | null; uptime_seconds?: number; cpu_millis?: number; memory_mb?: number } | null;
  observed_at?: string | null;
  id: string;
  state: 'preparing' | 'active' | 'draining';
  version: string;
  mount_enabled: boolean;
  pending_requests: number;
  created_at: string;
  activated_at: string | null;
}

export interface DeploymentListResponse {
  items: Deployment[];
  limit: number;
  offset: number;
  total?: number;
  summary?: {
    active: number;
    disabled: number;
    invocations: number;
    last_invoked_at: string | null;
  };
}

export interface ListDeploymentsParams {
  trigger_type?: TriggerType;
  enabled?: boolean;
  workflow_id?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface CreateDeploymentBody {
  cpu_millis?: number;
  memory_mb?: number;
  wf_id: string;
  name: string;
  slug: string;
  trigger_type: TriggerType;
  version_pin: VersionPin;
  pinned_major?: number;
  pinned_sub?: number;
  rate_limit_qps?: number;
  mount_enabled?: boolean;
  enabled?: boolean;
}

/** The one-shot create response. Plaintext credentials appear here ONCE
 * and never again — the UI must surface them prominently with a "this is
 * shown only once" warning.
 */
export interface CreateDeploymentResponse {
  id: string;
  provenance: ResourceProvenance;
  api_key?: string;
  hmac_secret?: string;
  endpoint_url?: string;
  webhook_url?: string;
}

export interface PatchDeploymentBody {
  cpu_millis?: number;
  memory_mb?: number;
  name?: string;
  enabled?: boolean;
  rate_limit_qps?: number;
  version_pin?: VersionPin;
  pinned_major?: number;
  pinned_sub?: number;
  mount_enabled?: boolean;
}

export interface RotateKeyResponse {
  api_key: string;
}

export interface TestInvokeResponse {
  invocation_id: string;
  execution_id: string;
  execution_url: string;
  result_url: string;
  status: string;
  outputs?: unknown;
  errors?: unknown;
  exec_time_ms?: number;
}

export type MetricsBucket = 'hour' | 'day';

export interface MetricsPoint {
  ts: string;
  calls: number;
  errors: number;
  latency_p50: number | null;
  latency_p95: number | null;
}

export interface MetricsResponse {
  series: MetricsPoint[];
  bucket: MetricsBucket;
  from: string;
  to: string;
}

export interface HistoryItem {
  id: string;
  status: string;
  task_type: string;
  source?: string;
  trigger_type?: TriggerType;
  submitted_at: string;
  started_at: string | null;
  finished_at: string | null;
  latency_ms?: number | null;
  error: string | null;
}

export interface HistoryResponse {
  items: HistoryItem[];
  next_cursor: string | null;
  limit: number;
}

// ---------------------------------------------------------------------------
// Internal: same `authedFetch` shim as `tasks.ts`. We re-implement it here
// rather than export from there — `tasks.ts` keeps it `function`-local; a
// cross-file import would force a refactor unrelated to T14.
// ---------------------------------------------------------------------------

import { getApiBase } from '@/lib/base-path';

const BASE = getApiBase();

async function authedFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const { useAuthStore } = await import('@/stores/auth');
  const token = useAuthStore.getState().token;
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (resp.status === 401) {
    useAuthStore.getState().handle401();
  }
  return resp;
}

function buildListQuery(p: ListDeploymentsParams): string {
  const params = new URLSearchParams();
  if (p.trigger_type) params.set('trigger_type', p.trigger_type);
  if (p.enabled !== undefined) params.set('enabled', String(p.enabled));
  if (p.workflow_id) params.set('workflow_id', p.workflow_id);
  if (p.q) params.set('q', p.q);
  if (p.limit !== undefined) params.set('limit', String(p.limit));
  if (p.offset !== undefined) params.set('offset', String(p.offset));
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

async function jsonOrThrow<T>(
  resp: Response,
  label: string,
): Promise<T> {
  if (!resp.ok) {
    // Best-effort detail extraction — the FastAPI default is
    // `{"detail": "..."}` for HTTPException, surface it to the caller
    // so the toast message is useful.
    let detail = '';
    try {
      const j = await resp.json();
      if (j && typeof j === 'object' && 'detail' in j) {
        detail = ` — ${String((j as { detail: unknown }).detail)}`;
      }
    } catch {
      // ignore non-JSON bodies
    }
    throw new Error(`${label} failed: ${resp.status} ${resp.statusText}${detail}`);
  }
  return (await resp.json()) as T;
}

// ---------------------------------------------------------------------------
// Public API.
// ---------------------------------------------------------------------------

export async function listDeployments(
  params: ListDeploymentsParams = {},
): Promise<DeploymentListResponse> {
  const resp = await authedFetch(
    `/api/v1/deployments${buildListQuery(params)}`,
  );
  return jsonOrThrow<DeploymentListResponse>(resp, 'listDeployments');
}

export async function getDeployment(id: string): Promise<Deployment> {
  const resp = await authedFetch(`/api/v1/deployments/${id}`);
  return jsonOrThrow<Deployment>(resp, 'getDeployment');
}

export async function createDeployment(
  body: CreateDeploymentBody,
): Promise<CreateDeploymentResponse> {
  const resp = await authedFetch('/api/v1/deployments', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return jsonOrThrow<CreateDeploymentResponse>(resp, 'createDeployment');
}

export async function patchDeployment(
  id: string,
  body: PatchDeploymentBody,
): Promise<Deployment> {
  const resp = await authedFetch(`/api/v1/deployments/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
  return jsonOrThrow<Deployment>(resp, 'patchDeployment');
}

export async function deleteDeployment(id: string): Promise<void> {
  const resp = await authedFetch(`/api/v1/deployments/${id}`, {
    method: 'DELETE',
  });
  if (!resp.ok) {
    throw new Error(
      `deleteDeployment failed: ${resp.status} ${resp.statusText}`,
    );
  }
}

export async function rotateKey(id: string): Promise<RotateKeyResponse> {
  const resp = await authedFetch(`/api/v1/deployments/${id}/rotate-key`, {
    method: 'POST',
  });
  return jsonOrThrow<RotateKeyResponse>(resp, 'rotateKey');
}

export async function testInvoke(
  id: string,
  inputs: unknown,
): Promise<TestInvokeResponse> {
  const resp = await authedFetch(`/api/v1/deployments/${id}/test-invoke`, {
    method: 'POST',
    body: JSON.stringify(inputs ?? {}),
  });
  return jsonOrThrow<TestInvokeResponse>(resp, 'testInvoke');
}

export interface MetricsParams {
  from: string;
  to: string;
  bucket?: MetricsBucket;
}

export async function getMetrics(
  id: string,
  params: MetricsParams,
): Promise<MetricsResponse> {
  const qs = new URLSearchParams();
  qs.set('from', params.from);
  qs.set('to', params.to);
  qs.set('bucket', params.bucket ?? 'hour');
  const resp = await authedFetch(
    `/api/v1/deployments/${id}/metrics?${qs.toString()}`,
  );
  return jsonOrThrow<MetricsResponse>(resp, 'getMetrics');
}

export interface HistoryParams {
  limit?: number;
  cursor?: string;
  status?: string[];
  from?: string;
  to?: string;
  order?: 'asc' | 'desc';
}

export async function getHistory(
  id: string,
  params: HistoryParams = {},
): Promise<HistoryResponse> {
  const qs = new URLSearchParams();
  if (params.limit !== undefined) qs.set('limit', String(params.limit));
  if (params.cursor) qs.set('cursor', params.cursor);
  for (const s of params.status ?? []) qs.append('status', s);
  if (params.from) qs.set('from', params.from);
  if (params.to) qs.set('to', params.to);
  if (params.order) qs.set('order', params.order);
  const tail = qs.toString();
  const resp = await authedFetch(
    `/api/v1/deployments/${id}/history${tail ? `?${tail}` : ''}`,
  );
  return jsonOrThrow<HistoryResponse>(resp, 'getHistory');
}
