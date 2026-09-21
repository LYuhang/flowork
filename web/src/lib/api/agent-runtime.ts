import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';

export type AgentRuntimeType = string;

export interface AgentRuntimeSettings {
  default_runtime_type: AgentRuntimeType;
  available_runtime_types: AgentRuntimeType[];
  runtime_options: Array<{
    id: AgentRuntimeType;
    label: string;
    description: string;
  }>;
  codex_managed_profile_id: string | null;
  preferred_timezone: string | null;
  codex_managed_profiles: Array<{
    id: string;
    name: string;
    model_count: number;
  }>;
  codex_auth_methods: Array<'chatgpt' | 'managed_api' | 'personal_api'>;
}

export interface CodexAccountStatus {
  cli_available: boolean;
  authenticated: boolean;
}

export interface CodexRateLimitWindow {
  used_percent: number;
  window_duration_mins: number | null;
  resets_at: number | null;
}

export interface CodexRateLimitBucket {
  limit_id: string;
  limit_name: string | null;
  plan_type: string | null;
  primary: CodexRateLimitWindow | null;
  secondary: CodexRateLimitWindow | null;
  credits: {
    has_credits: boolean;
    unlimited: boolean;
    balance: string | null;
  } | null;
  individual_limit: {
    limit: string;
    used: string;
    remaining_percent: number;
    resets_at: number;
  } | null;
  spend_control_reached: boolean | null;
  rate_limit_reached_type: string | null;
}

export interface CodexAccountUsage {
  email: string | null;
  plan_type: string | null;
  rate_limits: CodexRateLimitBucket[];
  rate_limit_reset_credits_available: number | null;
  usage_summary: {
    lifetime_tokens: number | null;
    peak_daily_tokens: number | null;
    longest_running_turn_sec: number | null;
    current_streak_days: number | null;
    longest_streak_days: number | null;
  } | null;
  daily_usage_buckets: Array<{ start_date: string; tokens: number }>;
  unavailable_sections: string[];
  fetched_at: string;
}

export interface CodexDeviceLogin {
  login_session_id: string;
  verification_url: string;
  user_code: string;
  expires_at: string;
}

export interface RuntimeReasoningEffortOption {
  id: string;
  label: string;
  description: string;
}

export interface RuntimeModelOption {
  id: string;
  label: string;
  description: string;
  api_source?: string | null;
  api_protocol?: string | null;
  provider: string | null;
  provider_model_id?: string | null;
  context_length?: number | null;
  input_modalities?: string[];
  output_modalities?: string[];
  supports_tools?: boolean | null;
  supports_web_search?: boolean;
  input_price?: string | null;
  output_price?: string | null;
  available?: boolean;
  is_default: boolean;
  supported_reasoning_efforts: RuntimeReasoningEffortOption[];
  default_reasoning_effort: string | null;
}

export interface AgentRuntimeCapabilities {
  protocol_version: 2;
  runtime_type: AgentRuntimeType;
  runtime_available: boolean;
  authenticated: boolean | null;
  source: string;
  models: RuntimeModelOption[];
  default_model_id: string | null;
  error_code: string | null;
  bound_agent_settings: {
    model_id: string | null;
    temperature: number | null;
    max_tokens: number | null;
    timeout: number | null;
    reasoning_effort: string | null;
  } | null;
}

async function authenticatedFetch(path: string, init?: RequestInit): Promise<Response> {
  const auth = useAuthStore.getState();
  const response = await fetch(`${getApiBase()}${path}`, {
    ...init,
    headers: {
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });
  if (response.status === 401) {
    auth.handle401();
    throw new Error('Authentication required');
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as {
      detail?: string | { code?: string };
    } | null;
    const detail = payload?.detail;
    throw new Error(
      typeof detail === 'string'
        ? detail
        : detail?.code ?? `Agent runtime request failed (${response.status})`,
    );
  }
  return response;
}

async function runtimeRequest(
  method: 'GET' | 'PUT',
  body?: Record<string, unknown>,
): Promise<AgentRuntimeSettings> {
  const auth = useAuthStore.getState();
  const response = await fetch(`${getApiBase()}/api/v1/agent-runtime/settings`, {
    method,
    headers: {
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
      Accept: 'application/json',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (response.status === 401) {
    auth.handle401();
    throw new Error('Authentication required');
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(
      typeof payload?.detail === 'string'
        ? payload.detail
        : `Runtime settings request failed (${response.status})`,
    );
  }
  return response.json() as Promise<AgentRuntimeSettings>;
}

export function getAgentRuntimeSettings(): Promise<AgentRuntimeSettings> {
  return runtimeRequest('GET');
}

export function setDefaultAgentRuntime(
  runtimeType: AgentRuntimeType,
): Promise<AgentRuntimeSettings> {
  return runtimeRequest('PUT', { default_runtime_type: runtimeType });
}

export function setPreferredTimezone(
  timezone: string,
): Promise<AgentRuntimeSettings> {
  return authenticatedFetch('/api/v1/agent-runtime/settings/timezone', {
    method: 'PUT',
    body: JSON.stringify({ preferred_timezone: timezone }),
  }).then((response) => response.json() as Promise<AgentRuntimeSettings>);
}

export async function selectCodexManagedProfile(
  profileId: string,
): Promise<AgentRuntimeSettings> {
  const response = await authenticatedFetch(
    '/api/v1/agent-runtime/codex/managed-profile',
    {
      method: 'PUT',
      body: JSON.stringify({ profile_id: profileId }),
    },
  );
  return response.json() as Promise<AgentRuntimeSettings>;
}

export async function getCodexAccountStatus(): Promise<CodexAccountStatus> {
  const response = await authenticatedFetch('/api/v1/agent-runtime/codex/account');
  return response.json() as Promise<CodexAccountStatus>;
}

export async function getCodexAccountUsage(): Promise<CodexAccountUsage> {
  const response = await authenticatedFetch(
    '/api/v1/agent-runtime/codex/account/usage',
  );
  return response.json() as Promise<CodexAccountUsage>;
}

export async function startCodexDeviceLogin(): Promise<CodexDeviceLogin> {
  const response = await authenticatedFetch(
    '/api/v1/agent-runtime/codex/account/device',
    { method: 'POST' },
  );
  return response.json() as Promise<CodexDeviceLogin>;
}

export async function disconnectCodexAccount(): Promise<CodexAccountStatus> {
  const response = await authenticatedFetch('/api/v1/agent-runtime/codex/account', {
    method: 'DELETE',
  });
  return response.json() as Promise<CodexAccountStatus>;
}

export async function getAgentRuntimeCapabilities(
  chatId?: string | null,
): Promise<AgentRuntimeCapabilities> {
  const params = new URLSearchParams();
  if (chatId) params.set('chat_id', chatId);
  const suffix = params.size > 0 ? `?${params.toString()}` : '';
  const response = await authenticatedFetch(
    `/api/v1/agent-runtime/capabilities${suffix}`,
  );
  return response.json() as Promise<AgentRuntimeCapabilities>;
}
