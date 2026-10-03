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

export type ChatAttachment = NonNullable<components['schemas']['MessagePostBody']['attachments']>[number];
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
  project_id?: string | null;
}

export interface ProjectWorkspace {
  workspace_scope_id: string;
  mount_scope_id: string;
  project_id: string;
}

export function useProjectWorkspace(projectId: string | null | undefined) {
  return useQuery({
    queryKey: ['project-workspace', projectId],
    enabled: !!projectId,
    staleTime: Infinity,
    queryFn: async (): Promise<ProjectWorkspace> => {
      const response = await fetch(`${getApiBase()}/api/v1/projects/${encodeURIComponent(projectId!)}/workspace`, {
        headers: authHeaders(),
      });
      if (response.status === 401) useAuthStore.getState().handle401();
      if (!response.ok) throw new Error(`project workspace failed: ${response.status}`);
      return response.json() as Promise<ProjectWorkspace>;
    },
  });
}

export interface ProjectSandboxStatus {
  project_id: string;
  mount_scope_id?: string | null;
  runtime_type?: string | null;
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

export type ChatListItem = components['schemas']['ChatListItem'] & {
  project_id?: string | null;
};

export interface ChatProject {
  project_id: string;
  name: string;
  runtime_type?: string | null;
  runtime_connection_id?: string | null;
  runtime_model_id?: string | null;
  chat_count: number;
  created_at: string;
  updated_at: string;
  last_activity_at?: string | null;
}

export interface DeleteChatProjectResult {
  project_id: string;
  deleted_chat_ids: string[];
  workspace_scope_id: string;
  vfs_deleted: number;
  runtime_state_deleted?: boolean;
}

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
export const CHAT_HISTORY_STALE_TIME_MS = 2 * 60 * 1000;
export const CHAT_HISTORY_GC_TIME_MS = 30 * 60 * 1000;

function authHeaders(): HeadersInit | undefined {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

export async function uploadChatAttachment(args: {
  scopeId: string;
  chatId: string;
  projectId?: string | null;
  file: File;
  type: ChatFileAttachmentType;
  signal?: AbortSignal;
}): Promise<ChatAttachment> {
  const body = new FormData();
  body.append('file', args.file, args.file.name);
  const base = getApiBase();
  const params = new URLSearchParams({ attachment_type: args.type });
  if (args.projectId) params.set('project_id', args.projectId);
  const res = await fetch(
    `${base}/api/v1/chat-scopes/${encodeURIComponent(args.scopeId)}` +
      `/chats/${encodeURIComponent(args.chatId)}/attachments?${params.toString()}`,
    { method: 'POST', headers: authHeaders(), body, signal: args.signal },
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

export async function fetchChatWorkspace(chatId: string): Promise<ChatWorkspace> {
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

export const useChatBootstrap = (surface: 'chat' | 'browser', enabled = true) =>
  useQuery({
    queryKey: ['chat-bootstrap', surface],
    queryFn: () => fetchGeneralChatBootstrap(surface),
    staleTime: 5 * 60 * 1000,
    enabled,
  });

export const useGeneralChatBootstrap = (enabled = true) => useChatBootstrap('chat', enabled);

export interface CreateChatSessionInput {
  scopeId: string;
  chatId: string;
  projectId?: string | null;
  workflowContext?: components['schemas']['WorkflowChatBinding'];
}

export const useCreateChatSession = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ scopeId, chatId, projectId, workflowContext }: CreateChatSessionInput): Promise<ChatListItem> => {
      const response = await fetch(
        `${getApiBase()}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`,
        {
          method: 'PUT',
          headers: { ...authHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_id: projectId ?? null, workflow_context: workflowContext }),
        },
      );
      if (response.status === 401) useAuthStore.getState().handle401();
      if (!response.ok) throw new Error(`Could not create chat (${response.status})`);
      return response.json() as Promise<ChatListItem>;
    },
    onSuccess: (chat, input) => {
      qc.setQueryData<ChatSessionsPage>(['chats', input.scopeId, chat.surface], (old) => {
        const others = old?.items.filter((item) => item.chat_id !== chat.chat_id) ?? [];
        return {
          items: [chat, ...others],
          total: old ? old.total + Number(!old.items.some((item) => item.chat_id === chat.chat_id)) : 1,
          limit: old?.limit ?? CHAT_SESSION_PAGE_SIZE,
          offset: old?.offset ?? 0,
        };
      });
      void qc.invalidateQueries({ queryKey: ['chats', input.scopeId] });
      void qc.invalidateQueries({ queryKey: ['chat-projects'] });
    },
  });
};

async function projectRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(authHeaders());
  if (init?.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(`${getApiBase()}/api/v1/projects${path}`, {
    ...init,
    headers,
  });
  if (response.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!response.ok) {
    let message = `project request failed: ${response.status}`;
    try {
      const payload = await response.json() as { detail?: string | { message?: string } };
      message = typeof payload.detail === 'string'
        ? payload.detail
        : payload.detail?.message ?? message;
    } catch {
      // Keep the stable status fallback when the response is not JSON.
    }
    throw new Error(message);
  }
  return await response.json() as T;
}

export const useChatProjects = (enabled = true) => useQuery({
  queryKey: ['chat-projects'],
  queryFn: () => projectRequest<ChatProject[]>(''),
  enabled,
});

export interface ProjectMcpSelection {
  mcp_server_ids: string[];
  mcp_config_revision: number;
}

export const useProjectMcpSelection = (projectId: string | null | undefined, enabled = true) => useQuery({
  queryKey: ['project-mcp', projectId],
  enabled: !!projectId && enabled,
  queryFn: () => projectRequest<ProjectMcpSelection>(`/${encodeURIComponent(projectId!)}/mcp`),
});

export const useSetProjectMcpSelection = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, selection }: { projectId: string; selection: ProjectMcpSelection }) => {
      return projectRequest<ProjectMcpSelection>(`/${encodeURIComponent(projectId)}/mcp`, {
        method: 'PUT', body: JSON.stringify(selection),
      });
    },
    onSuccess: (selection, { projectId }) => qc.setQueryData(['project-mcp', projectId], selection),
    onSettled: (_data, _error, { projectId }) => qc.invalidateQueries({ queryKey: ['project-mcp', projectId] }),
  });
};

export const useProjectSandboxStatuses = (projectIds: string[]) => useQuery({
  queryKey: ['project-sandboxes', projectIds],
  enabled: projectIds.length > 0,
  refetchInterval: 5000,
  queryFn: () => {
    const params = new URLSearchParams();
    for (const id of projectIds) params.append('project_id', id);
    return projectRequest<{ items: ProjectSandboxStatus[] }>(`/sandboxes?${params}`);
  },
});

export const useProjectSandboxAction = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, action }: { projectId: string; action: 'start' | 'release' }) =>
      projectRequest<ProjectSandboxStatus>(`/${encodeURIComponent(projectId)}/sandbox`, {
        method: action === 'start' ? 'POST' : 'DELETE',
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['project-sandboxes'] });
    },
  });
};

export const useCreateChatProject = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => projectRequest<ChatProject>('', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
    onSuccess: (project) => {
      qc.setQueryData<ChatProject[]>(['chat-projects'], (current = []) => [
        project,
        ...current.filter((item) => item.project_id !== project.project_id),
      ]);
    },
  });
};

export const useRenameChatProject = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, name }: { projectId: string; name: string }) =>
      projectRequest<ChatProject>(`/${encodeURIComponent(projectId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ name }),
      }),
    onSuccess: (project) => {
      qc.setQueryData<ChatProject[]>(['chat-projects'], (current = []) => current.map(
        (item) => item.project_id === project.project_id
          ? { ...item, ...project, chat_count: item.chat_count }
          : item,
      ));
    },
  });
};

export const useDeleteChatProject = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) => projectRequest<DeleteChatProjectResult>(
      `/${encodeURIComponent(projectId)}`,
      { method: 'DELETE' },
    ),
    onSuccess: (result) => {
      qc.setQueryData<ChatProject[]>(['chat-projects'], (current = []) => current.filter(
        (item) => item.project_id !== result.project_id,
      ));
      void qc.invalidateQueries({ queryKey: ['chats'] });
      void qc.invalidateQueries({ queryKey: ['project-sandboxes'] });
      void qc.removeQueries({ queryKey: ['chat-history'], exact: false });
      qc.removeQueries({ queryKey: ['project-mcp', result.project_id] });
      qc.removeQueries({ queryKey: ['project-workspace', result.project_id] });
      for (const chatId of result.deleted_chat_ids) {
        void qc.removeQueries({ queryKey: ['chat-workspace', chatId] });
      }
      void qc.invalidateQueries({ queryKey: ['vfs'] });
    },
  });
};

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
    // A lost extension lease expires server-side after its reconnect grace.
    // Refresh only while one exists, so a stale history snapshot cannot keep
    // the composer permanently locked as if another window still owned it.
    refetchInterval: (query) => surface === 'browser'
      && query.state.data?.items.some((item) => item.browser_control_status === 'lost')
      ? 5000 : false,
  });

export async function deleteChatSession(
  scopeId: string,
  chatId: string,
  surface: 'chat' | 'browser' = 'chat',
  deleteFiles = false,
): Promise<DeleteChatResult> {
  const base = getApiBase();
  const params = new URLSearchParams({ surface, delete_files: String(deleteFiles) });
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

export const useDeleteChatSession = (scopeId: string | null, surface: 'chat' | 'browser' = 'chat', deleteFiles = false) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (chatId: string) => deleteChatSession(scopeId as string, chatId, surface, deleteFiles),
    onSuccess: (_result, chatId) => {
      void qc.invalidateQueries({ queryKey: ['chats', scopeId, surface] });
      void qc.invalidateQueries({ queryKey: ['chat-projects'] });
      void qc.removeQueries({ queryKey: ['chat-history', scopeId, chatId] });
      void qc.removeQueries({ queryKey: ['chat-workspace', chatId] });
      void qc.invalidateQueries({ queryKey: ['vfs'] });
    },
  });
};

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
    // Recent transcripts are a bounded in-memory L1 cache. Durable Turn
    // completion and the 30-second server reconcile explicitly invalidate
    // this key, so a longer freshness window removes navigation refetches
    // without hiding committed messages from another tab for long.
    staleTime: CHAT_HISTORY_STALE_TIME_MS,
    gcTime: CHAT_HISTORY_GC_TIME_MS,
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
      // Finished jobs remain in history. Their presence must not keep a chat
      // polling every second forever; reconciliation still discovers new jobs.
      return jobs.some((job) => (
        job.status === 'queued' || job.status === 'running' || job.status === 'cancelling'
      )) ? 1000 : false;
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

/** Resolve a referenced task independently of the bounded recent-task list. */
export function useReferencedBackgroundJob(scopeId: string, chatId: string, jobId?: string) {
  return useQuery({
    queryKey: ['background-jobs', scopeId, chatId, 'reference', jobId],
    enabled: !!scopeId && !!chatId && !!jobId,
    retry: false,
    queryFn: async (): Promise<BackgroundJob | null> => {
      const response = await fetch(`${getApiBase()}/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}/background-jobs/${encodeURIComponent(jobId!)}`, { headers: authHeaders() });
      if (response.status === 401) useAuthStore.getState().handle401();
      if (response.status === 404 || response.status === 403) return null;
      if (!response.ok) throw new Error(`background task lookup failed: ${response.status}`);
      return response.json() as Promise<BackgroundJob>;
    },
  });
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
