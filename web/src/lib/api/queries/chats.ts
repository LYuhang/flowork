/**
 * Chat session + history queries.
 *
 * Two siblings, both scoped to a chat carrier scope:
 *   - `useChatSessions(scopeId)` lists every chat under a carrier scope
 *     (`GET /api/v1/chat-scopes/{scope_id}/chats`).
 *   - `useChatHistory(scopeId, chatId)` reads the transcript for one chat
 *     (`GET /api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages`).
 *
 * Both queries throw on `error` so TanStack Query lands them in `isError`
 * (and ErrorBoundary picks them up via `app/providers.tsx`). The query
 * keys are intentionally compact (`['chats', scopeId]` / `['chat-history',
 * scopeId, chatId]`) so T10's SSE handlers can invalidate them with a single
 * prefix invalidation when new turns commit.
 *
 * `enabled: !!scopeId` (and `!!chatId` for history) guards the typical
 * "no chat scope selected yet" or "no active chat yet" state — the
 * AgentChatSidebar renders nothing in the former case, and lazily kicks
 * off the latter once the user picks a session.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import type {
  SandboxLifecycleStatus,
  SandboxResourceStatus,
} from '@/lib/sandbox-status';
import type { TodoItem } from '@/stores/chat-stream';
import type { components } from '@/lib/api/schema';

export type ChatAttachment = components['schemas']['Attachment'];
export type ChatFileAttachmentType = 'file' | 'image' | 'video';

export interface ChatBootstrap {
  carrier_scope_id: string;
  surface: 'chat' | 'browser';
  available_commands: string[];
  debug_view_enabled?: boolean;
}

export interface ChatWorkspace {
  workspace_scope_id: string;
  mount_scope_id?: string | null;
  chat_id: string;
  current_workflow_id?: string | null;
}

export interface ChatSandboxStatusItem {
  chat_id: string;
  scope_id: string;
  status: SandboxLifecycleStatus;
  lifecycle_state?: 'warm' | 'hibernating' | 'hibernated' | 'restoring' | 'releasing' | 'snapshot_failed' | 'released' | 'closed';
  activity_state?: 'busy' | 'idle' | 'unknown';
  inflight_operations?: number;
  idle_elapsed_s?: number | null;
  idle_for_s?: number | null;
  ttl_phase?: 'warm_idle' | 'idle_release' | 'snapshot_retention' | null;
  ttl_s?: number | null;
  ttl_paused?: boolean;
  ttl_remaining_s?: number | null;
  next_transition?: 'hibernate' | 'warm' | 'release' | null;
  observed_at_unix_s?: number;
  closed_for_s?: number | null;
  resources?: SandboxResourceStatus;
}

export interface DeleteChatResult {
  chat_id: string;
  workspace_scope_id: string;
  vfs_deleted: number;
  runtime_state_deleted?: boolean;
}

export type ChatListItem = components['schemas']['ChatListItem'];

export interface ChatSessionsPage {
  items: ChatListItem[];
  total: number;
  limit: number;
  offset: number;
}

const CHAT_SESSION_PAGE_SIZE = 500;

export class ChatDeleteError extends Error {
  readonly code: string;

  constructor(
    code: string,
    message: string,
  ) {
    super(message);
    this.code = code;
    this.name = 'ChatDeleteError';
  }
}

export interface ChatState {
  todo_items: TodoItem[];
  background_jobs: BackgroundJob[];
  active_modes: string[];
  mcp_server_ids: string[];
  mcp_config_revision: number;
}

export interface BackgroundJob {
  job_id: string;
  chat_id: string;
  parent_run_id?: string | null;
  runtime_type: string;
  executor_type: string;
  tool_name: string;
  title: string;
  status:
    | 'queued'
    | 'running'
    | 'cancelling'
    | 'completed'
    | 'failed'
    | 'cancelled';
  progress: {
    current: number;
    total?: number | null;
    message: string;
  };
  input: Record<string, unknown>;
  result: Record<string, unknown>;
  result_ref?: string | null;
  error: Record<string, unknown>;
  event_seq: number;
  cancel_requested: boolean;
  delivery_status: 'pending' | 'delivered';
  delivered_at?: string | null;
  delivery_batch_id?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string | null;
}

export type BackgroundJobFilter =
  | 'current'
  | 'all'
  | 'active'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface ChatHistoryPage {
  items: unknown[];
  total: number;
  limit: number;
  offset: number;
}

export interface FetchChatHistoryPageOptions {
  limit?: number;
  offset?: number;
  tail?: boolean;
  beforeTurnId?: string | null;
  debug?: boolean;
}

// A history row can contain encrypted tool payloads and rich artifacts. The
// first screen needs the recent conversation, not 200 decrypted rows. Older
// messages remain available through the existing explicit "load earlier"
// path, which fetches larger windows only when requested.
export const CHAT_INITIAL_HISTORY_LIMIT = 30;

function authHeaders(): HeadersInit | undefined {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

export async function uploadChatAttachment(args: {
  scopeId: string;
  chatId: string;
  file: File;
  type: ChatFileAttachmentType;
}): Promise<ChatAttachment> {
  const body = new FormData();
  body.append('file', args.file, args.file.name);
  const base = getApiBase();
  const params = new URLSearchParams({ attachment_type: args.type });
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(args.scopeId)}` +
      `/chats/${encodeURIComponent(args.chatId)}/attachments?${params.toString()}`,
    { method: 'POST', headers: authHeaders(), body },
  );
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) {
    let detail = `attachment upload failed: ${res.status}`;
    try {
      const payload = await res.json() as { detail?: unknown };
      if (typeof payload.detail === 'string') detail = payload.detail;
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new Error(detail);
  }
  return await res.json() as ChatAttachment;
}

async function fetchGeneralChatBootstrap(surface: 'chat' | 'browser' = 'chat'): Promise<ChatBootstrap> {
  const base = getApiBase();
  const params = new URLSearchParams({ surface });
  const res = await fetch(`${base}/api/v1/chats/bootstrap?${params.toString()}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) {
    throw new Error(`chat bootstrap failed: ${res.status}`);
  }
  return (await res.json()) as ChatBootstrap;
}

async function fetchChatWorkspace(chatId: string): Promise<ChatWorkspace> {
  const base = getApiBase();
  const params = new URLSearchParams({ chat_id: chatId });
  const res = await fetch(`${base}/api/v1/chats/workspace?${params.toString()}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) {
    throw new Error(`chat workspace failed: ${res.status}`);
  }
  return (await res.json()) as ChatWorkspace;
}

export const useChatBootstrap = (surface: 'chat' | 'browser') =>
  useQuery({
    queryKey: ['chat-bootstrap', surface],
    queryFn: () => fetchGeneralChatBootstrap(surface),
    staleTime: 5 * 60 * 1000,
  });

export const useGeneralChatBootstrap = () => useChatBootstrap('chat');

export const useBrowserChatBootstrap = (enabled = true) =>
  useQuery({
    queryKey: ['chat-bootstrap', 'browser'],
    enabled,
    queryFn: () => fetchGeneralChatBootstrap('browser'),
    staleTime: 5 * 60 * 1000,
  });

export const useChatWorkspace = (chatId: string | null) =>
  useQuery({
    queryKey: ['chat-workspace', chatId],
    queryFn: () => fetchChatWorkspace(chatId as string),
    enabled: !!chatId,
    staleTime: 5 * 60 * 1000,
  });

export async function fetchAllChatSessions(
  scopeId: string,
  surface: 'chat' | 'browser' = 'chat',
): Promise<ChatSessionsPage> {
  const base = getApiBase();
  const items: ChatListItem[] = [];
  let total: number;
  let offset = 0;

  do {
    const params = new URLSearchParams({
      surface,
      limit: String(CHAT_SESSION_PAGE_SIZE),
      offset: String(offset),
    });
    const res = await fetch(
      `${base}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats?${params.toString()}`,
      { headers: authHeaders() },
    );
    if (res.status === 401) {
      useAuthStore.getState().handle401();
      throw new Error('auth');
    }
    if (!res.ok) throw new Error(`chat sessions failed: ${res.status}`);
    const page = await res.json() as ChatSessionsPage;
    items.push(...page.items);
    total = page.total;
    if (page.items.length === 0) break;
    offset += page.items.length;
  } while (items.length < total);

  return { items, total, limit: items.length, offset: 0 };
}

export const useChatSessions = (scopeId: string | null, surface: 'chat' | 'browser' = 'chat') =>
  useQuery({
    queryKey: ['chats', scopeId, surface],
    enabled: !!scopeId,
    queryFn: () => fetchAllChatSessions(scopeId!, surface),
  });

export async function deleteChatSession(
  scopeId: string,
  chatId: string,
  surface: 'chat' | 'browser' = 'chat',
): Promise<DeleteChatResult> {
  const base = getApiBase();
  const params = new URLSearchParams({ surface });
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}?${params.toString()}`,
    { method: 'DELETE', headers: authHeaders() },
  );
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) {
    let code = 'delete_failed';
    let message = `delete chat failed: ${res.status}`;
    try {
      const payload = await res.json() as {
        detail?: string | { error_code?: string; message?: string };
      };
      if (typeof payload.detail === 'string') message = payload.detail;
      if (payload.detail && typeof payload.detail === 'object') {
        code = payload.detail.error_code || code;
        message = payload.detail.message || message;
      }
    } catch {
      // Preserve the stable status-based fallback when the server body is not JSON.
    }
    throw new ChatDeleteError(code, message);
  }
  return (await res.json()) as DeleteChatResult;
}

export async function renameChatSession(
  scopeId: string,
  chatId: string,
  name: string,
): Promise<ChatListItem> {
  const base = getApiBase();
  const headers = new Headers(authHeaders());
  headers.set('Content-Type', 'application/json');
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`,
    {
      method: 'PATCH',
      headers,
      body: JSON.stringify({ name }),
    },
  );
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) throw new Error(`rename chat failed: ${res.status}`);
  return await res.json() as ChatListItem;
}

export const useRenameChatSession = (
  scopeId: string | null,
  surface: 'chat' | 'browser' = 'chat',
) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ chatId, name }: { chatId: string; name: string }) =>
      renameChatSession(scopeId as string, chatId, name),
    onSuccess: (renamed) => {
      qc.setQueryData<{ items?: ChatListItem[] }>(
        ['chats', scopeId, surface],
        (current) => current
          ? {
              ...current,
              items: (current.items ?? []).map((item) =>
                item.chat_id === renamed.chat_id ? { ...item, ...renamed } : item,
              ),
            }
          : current,
      );
    },
  });
};

export const useDeleteChatSession = (scopeId: string | null, surface: 'chat' | 'browser' = 'chat') => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (chatId: string) => deleteChatSession(scopeId as string, chatId, surface),
    onSuccess: (_result, chatId) => {
      void qc.invalidateQueries({ queryKey: ['chats', scopeId, surface] });
      void qc.invalidateQueries({ queryKey: ['chat-sandbox-statuses'] });
      void qc.removeQueries({ queryKey: ['chat-history', scopeId, chatId] });
      void qc.removeQueries({ queryKey: ['chat-workspace', chatId] });
      void qc.invalidateQueries({ queryKey: ['vfs'] });
    },
  });
};

export const useChatSandboxStatuses = (chatIds: string[]) =>
  useQuery({
    queryKey: ['chat-sandbox-statuses', chatIds],
    enabled: chatIds.length > 0,
    refetchInterval: 5000,
    queryFn: async () => {
      const base = getApiBase();
      const params = new URLSearchParams();
      for (const id of chatIds) params.append('chat_id', id);
      const res = await fetch(`${base}/api/v1/chats/sandboxes?${params.toString()}`, {
        headers: authHeaders(),
      });
      if (res.status === 401) {
        useAuthStore.getState().handle401();
        throw new Error('auth');
      }
      if (!res.ok) {
        throw new Error(`chat sandbox statuses failed: ${res.status}`);
      }
      return (await res.json()) as { items: ChatSandboxStatusItem[] };
    },
  });

export async function fetchChatHistoryPage(
  scopeId: string,
  chatId: string,
  options: FetchChatHistoryPageOptions = {},
): Promise<ChatHistoryPage> {
  const base = getApiBase();
  const params = new URLSearchParams({
    limit: String(options.limit ?? 200),
    offset: String(options.offset ?? 0),
  });
  if (options.tail) params.set('tail', 'true');
  if (options.beforeTurnId) params.set('before_turn_id', options.beforeTurnId);
  if (options.debug) params.set('debug', 'true');
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}/messages?${params.toString()}`,
    { headers: authHeaders() },
  );
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) throw new Error(`chat history failed: ${res.status}`);

  const page = await res.json();
  return {
    items: Array.isArray(page.items) ? page.items : [],
    total: typeof page.total === 'number' ? page.total : 0,
    limit: typeof page.limit === 'number' ? page.limit : options.limit ?? 200,
    offset: typeof page.offset === 'number' ? page.offset : options.offset ?? 0,
  };
}

export async function fetchChatHistory(
  scopeId: string,
  chatId: string,
  beforeTurnId?: string | null,
) {
  return fetchChatHistoryPage(scopeId, chatId, {
    limit: CHAT_INITIAL_HISTORY_LIMIT,
    tail: true,
    beforeTurnId,
  });
}

export const useChatHistory = (
  scopeId: string | null,
  chatId: string | null,
  enabled = true,
  beforeTurnId?: string | null,
) =>
  useQuery({
    queryKey: ['chat-history', scopeId, chatId, beforeTurnId ?? null],
    enabled: enabled && !!(scopeId && chatId),
    queryFn: () => fetchChatHistory(scopeId!, chatId!, beforeTurnId),
    // Keep the already-rendered transcript while the same Chat moves from its
    // normal tail query to the active-Turn boundary query. Never borrow data
    // from another Chat: that would briefly display the wrong conversation.
    placeholderData: (previousData, previousQuery) => {
      const previousKey = previousQuery?.queryKey;
      return previousKey?.[1] === scopeId && previousKey?.[2] === chatId
        ? previousData
        : undefined;
    },
    staleTime: 15 * 1000,
  });

export async function fetchChatState(scopeId: string, chatId: string): Promise<ChatState> {
  const base = getApiBase();
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}/state`,
    { headers: authHeaders() },
  );
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) throw new Error(`chat state failed: ${res.status}`);
  return (await res.json()) as ChatState;
}

export const useChatState = (
  scopeId: string | null,
  chatId: string | null,
  enabled = true,
) =>
  useQuery({
    queryKey: ['chat-state', scopeId, chatId],
    enabled: enabled && !!(scopeId && chatId),
    queryFn: () => fetchChatState(scopeId!, chatId!),
    retry: false,
    refetchInterval: (query) => {
      const jobs = query.state.data?.background_jobs ?? [];
      return jobs.length > 0 ? 1000 : false;
    },
  });

export async function cancelBackgroundJob(
  scopeId: string,
  chatId: string,
  jobId: string,
): Promise<BackgroundJob> {
  const base = getApiBase();
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}/background-jobs/${encodeURIComponent(jobId)}/cancel`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(authHeaders() ?? {}),
      },
      body: JSON.stringify({ reason: 'user_requested' }),
    },
  );
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) throw new Error(`background job cancel failed: ${res.status}`);
  return (await res.json()) as BackgroundJob;
}

export async function fetchBackgroundJobs(
  scopeId: string,
  chatId: string,
  filter: BackgroundJobFilter = 'current',
): Promise<BackgroundJob[]> {
  const base = getApiBase();
  const status = filter === 'active'
    ? 'queued,running,cancelling'
    : filter === 'current'
      ? 'all'
    : filter;
  const query = status === 'all' ? '' : `?status=${encodeURIComponent(status)}`;
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}/background-jobs${query}`,
    { headers: authHeaders() },
  );
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) throw new Error(`background job list failed: ${res.status}`);
  const jobs = (await res.json()) as BackgroundJob[];
  return filter === 'current'
    ? jobs.filter((job) => (
      job.delivery_status !== 'delivered' || [
        'queued',
        'running',
        'cancelling',
      ].includes(job.status)
    ))
    : jobs;
}

export function useBackgroundJobs(
  scopeId: string | null,
  chatId: string | null,
  filter: BackgroundJobFilter = 'current',
) {
  return useQuery({
    queryKey: ['background-jobs', scopeId, chatId, filter],
    enabled: !!scopeId && !!chatId,
    queryFn: () => fetchBackgroundJobs(scopeId!, chatId!, filter),
    // Durable SSE is the low-latency path. This slow visible-View reconcile is
    // only a safety net for browser suspension/network transitions that can
    // sever a stream without delivering its final cursor.
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  });
}
