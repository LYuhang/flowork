/**
 * SSE event → app-state router.
 *
 * One central dispatcher so the `agent-stream.ts` fetch handler doesn't
 * grow a big switch. Each backend SSE frame is mapped to one or more of:
 *   - mutations on `useChatStreamStore` (streaming chunks, lifecycle)
 *   - TanStack Query invalidations via the module-scope `queryClient`
 *
 * The 7 event names handled here come straight from
 * `api/src/vibecanvas_api/streaming/sse.py`:
 *   - `started`       → begin a turn (allocate turnId).
 *   - `CHAT_UPDATE`   → push one assistant/tool chunk to the live buffer.
 *   - `VIBE_ACTION`   → workflow mutations (full handling lands in T12).
 *                       For now we invalidate the workflow cache so the
 *                       canvas refetches the post-action state.
 *   - `WORKFLOW_SYNC` → full workflow snapshot pushed by the agent's
 *                       `show_workflow` tool; same treatment as VIBE_ACTION
 *                       for now (invalidate; T12 may upgrade to setQueryData).
 *   - `META_SYNC`     → workflow meta (version pointer) changed; refetch.
 *   - `EXEC_UPDATE`   → node-execution stream (T13 wires the dedicated
 *                       exec-stream store; deliberately a no-op here).
 *   - `HISTORY_SYNC`  → server-pushed full history replay; rare. We just
 *                       invalidate so the persisted query re-runs.
 *   - `done`          → clean termination; flip state and refetch history
 *                       plus the chat-sessions list (backend auto-creates
 *                       a session on first user message).
 *   - `error`         → engine_error flips state to failed; cancelled is a
 *                       normal terminal state for user-requested Stop.
 *
 * Payloads are typed as `unknown` and narrowed per-case to keep the
 * router strict — we never trust the wire blob's shape without a check.
 */
import type { QueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { queryClient } from '@/app/query-client';
import i18n from '@/lib/i18n';
import { useChatStreamStore } from '@/stores/chat-stream';
import type { ChatStreamEvent, StreamChunk, TodoItem } from '@/stores/chat-stream';
import type { ToolInvocationEnvelope } from '@/components/agent-sidebar/types';
import { fetchChatHistory, type ChatHistoryPage } from '@/lib/api/queries/chats';
import { rememberActiveTurn, clearActiveTurn } from './active-turn';

export interface RouteSignalContext {
  wfId: string;
  chatId: string;
}

export interface RouteSignalPresentation {
  showNotice: (level: 'info' | 'warning' | 'error', message: string) => void;
  /** Production terminal handoff. Kept injectable so the pure event-router
   * tests never perform network I/O. */
  loadDurableHistory?: (
    wfId: string,
    chatId: string,
  ) => Promise<ChatHistoryPage>;
}

const defaultPresentation: RouteSignalPresentation = {
  showNotice: (level, message) => {
    if (level === 'error') toast.error(message);
    else if (level === 'warning') toast.warning(message);
    else toast.info(message);
  },
};

function handoffToDurableHistory(
  client: QueryClient,
  ctx: RouteSignalContext,
  turnId: string | null,
  presentation: RouteSignalPresentation,
): void {
  if (!turnId || !presentation.loadDurableHistory) return;
  void presentation.loadDurableHistory(ctx.wfId, ctx.chatId).then((history) => {
    // Install the canonical transcript before releasing the live projection.
    // React therefore observes either (pre-Turn durable history + live Turn) or durable
    // head, never both complete copies and never an empty seam between them.
    client.setQueryData(
      ['chat-history', ctx.wfId, ctx.chatId, null],
      history,
    );
    useChatStreamStore.getState().finishProjection(ctx.chatId, turnId);
  }).catch(() => {
    // Keep the already-rendered live projection authoritative until a later
    // reconciliation can load the database transcript. Do not guess equality
    // from message text and do not discard a visible completed response.
    client.invalidateQueries({
      queryKey: ['chat-history', ctx.wfId, ctx.chatId, null],
    });
  });
}

/** Type guard: payload is a non-null object we can probe with `in`. */
function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null;
}

/** Narrow a `started` payload to its `turn_id`. */
function getTurnId(payload: unknown): string | null {
  if (isObject(payload) && typeof payload.turn_id === 'string') {
    return payload.turn_id;
  }
  return null;
}

/**
 * Narrow a CHAT_UPDATE payload to the streamable chunk.
 *
 * Backend always sends `{ message: { role, content, tool_calls?, ... } }`,
 * but we defensively fall back to treating the payload itself as the chunk
 * if a future emitter elides the wrapper.
 */
function getChunk(payload: unknown): StreamChunk | null {
  if (!isObject(payload)) return null;
  const candidate = isObject(payload.message) ? payload.message : payload;
  if (
    isObject(candidate) &&
    typeof candidate.role === 'string' &&
    typeof candidate.content === 'string'
  ) {
    return {
      role: candidate.role,
      content: candidate.content,
      tool_calls: Array.isArray(candidate.tool_calls)
        ? (candidate.tool_calls as unknown[])
        : undefined,
      tool_call_id:
        typeof candidate.tool_call_id === 'string'
          ? candidate.tool_call_id
          : undefined,
      artifact: isObject(candidate.artifact)
        ? (candidate.artifact as Record<string, unknown>)
        : undefined,
    };
  }
  return null;
}

function getChatEvent(payload: unknown): ChatStreamEvent | null {
  if (!isObject(payload) || typeof payload.type !== 'string') return null;
  const type = payload.type;
  if (
    (type === 'message_start' || type === 'message_replace') &&
    typeof payload.message_id === 'string'
  ) {
    if (type === 'message_start') {
      if (payload.role === 'system') return null;
      const content = typeof payload.content === 'string' ? payload.content : '';
      return {
        type,
        message_id: payload.message_id,
        role: 'assistant',
        content: content || undefined,
        artifact: isObject(payload.artifact) ? payload.artifact : undefined,
        parts: Array.isArray(payload.parts) ? payload.parts as Array<Record<string, unknown>> : undefined,
      };
    }
    const content = typeof payload.content === 'string' ? payload.content : '';
    return {
      type,
      message_id: payload.message_id,
      content,
      artifact: isObject(payload.artifact) ? payload.artifact : undefined,
      parts: Array.isArray(payload.parts) ? payload.parts as Array<Record<string, unknown>> : undefined,
    };
  }
  if (type === 'message_delta' && typeof payload.message_id === 'string') {
    return {
      type,
      message_id: payload.message_id,
      delta: typeof payload.delta === 'string' ? payload.delta : '',
    };
  }
  if (type === 'message_end' && typeof payload.message_id === 'string') {
    return { type, message_id: payload.message_id };
  }
  if (
    type === 'tool_start' &&
    typeof payload.message_id === 'string' &&
    typeof payload.tool_call_id === 'string' &&
    typeof payload.name === 'string'
  ) {
    return {
      type,
      message_id: payload.message_id,
      tool_call_id: payload.tool_call_id,
      name: payload.name,
      arguments: typeof payload.arguments === 'string' ? payload.arguments : undefined,
      artifact: isObject(payload.artifact) ? payload.artifact : undefined,
      parts: Array.isArray(payload.parts) ? payload.parts as Array<Record<string, unknown>> : undefined,
    };
  }
  if (type === 'tool_delta' && typeof payload.tool_call_id === 'string') {
    return {
      type,
      tool_call_id: payload.tool_call_id,
      arguments_delta: typeof payload.arguments_delta === 'string' ? payload.arguments_delta : undefined,
    };
  }
  if (type === 'tool_update' && typeof payload.tool_call_id === 'string') {
    return {
      type,
      tool_call_id: payload.tool_call_id,
      content: typeof payload.content === 'string' ? payload.content : undefined,
      artifact: isObject(payload.artifact) ? payload.artifact : undefined,
      status:
        payload.status === 'done' || payload.status === 'error' || payload.status === 'running'
          ? payload.status
          : undefined,
    };
  }
  if (type === 'tool_end' && typeof payload.tool_call_id === 'string') {
    return {
      type,
      tool_call_id: payload.tool_call_id,
      content: typeof payload.content === 'string' ? payload.content : '',
      invocation: isObject(payload.invocation)
        ? payload.invocation as unknown as ToolInvocationEnvelope
        : undefined,
      artifact: isObject(payload.artifact) ? payload.artifact : undefined,
      status: payload.status === 'error' ? 'error' : 'done',
    };
  }
  if (type === 'todo_update') {
    const items = getTodoItems(payload.items);
    if (items) return { type, items };
  }
  return null;
}

function getTodoItems(raw: unknown): TodoItem[] | null {
  if (!Array.isArray(raw)) return null;
  const items: TodoItem[] = [];
  for (const item of raw) {
    if (!isObject(item)) continue;
    const status = item.status;
    if (status !== 'pending' && status !== 'in_progress' && status !== 'done') continue;
    const id = typeof item.id === 'number' ? item.id : Number(item.id);
    if (!Number.isFinite(id)) continue;
    items.push({
      id,
      text: typeof item.text === 'string' ? item.text : String(item.text ?? ''),
      status,
    });
  }
  return items;
}

function toolNameFromArtifact(artifact: Record<string, unknown> | null | undefined): string | null {
  const meta = artifact?.meta;
  if (isObject(meta) && typeof meta.tool === 'string' && meta.tool) return meta.tool;
  return null;
}

function getWorkflowId(payload: unknown): string | null {
  if (!isObject(payload)) return null;
  if (typeof payload.workflow_id === 'string' && payload.workflow_id) {
    return payload.workflow_id;
  }
  const meta = payload.meta;
  if (isObject(meta) && typeof meta.workflow_id === 'string' && meta.workflow_id) {
    return meta.workflow_id;
  }
  return null;
}

function parseWorkflowIdFromString(content: string): string | null {
  if (!content.trim().startsWith('{')) return null;
  try {
    return getWorkflowId(JSON.parse(content));
  } catch {
    return null;
  }
}

function workflowIdFromToolArtifact(
  artifact: Record<string, unknown> | null | undefined,
): string | null {
  if (!artifact) return null;
  const direct = getWorkflowId(artifact);
  if (direct) return direct;

  const nested = artifact.artifact;
  if (isObject(nested)) {
    const nestedId = getWorkflowId(nested);
    if (nestedId) return nestedId;
    const handles = nested.handles;
    if (isObject(handles)) {
      const handleId = getWorkflowId(handles);
      if (handleId) return handleId;
    }
  }

  const payload = artifact.payload;
  if (isObject(payload)) {
    const payloadId = getWorkflowId(payload);
    if (payloadId) return payloadId;
  }

  const content = artifact.content;
  if (typeof content === 'string') {
    const contentId = parseWorkflowIdFromString(content);
    if (contentId) return contentId;
  }
  return null;
}

function invalidateWorkflow(client: QueryClient, payload: unknown, fallbackWfId: string) {
  const wfId = getWorkflowId(payload) ?? fallbackWfId;
  for (const key of ['workflow', 'workflow-head', 'workflow-versions']) {
    client.invalidateQueries({ queryKey: [key, wfId] });
  }
}

const VFS_MUTATING_TOOLS = new Set([
  'write_file',
  'edit_file',
  'bash',
  'shell',
  'exec_command',
  'apply_patch',
  'file_change',
  'batch_execute',
  'node_execute',
  'run_workflow',
  'update_canvas',
  'get_workflow',
]);

/**
 * Dynamic Island status relay.
 *
 * When this chat UI runs framed inside the extension side panel (`/embed`),
 * the island overlay needs to reflect the agent's CHAT-driven phase
 * (thinking / browser-tool / non-browser-tool / streaming / ready). These
 * product phases are known here on the chat SSE, while the Playwright relay is
 * deliberately only a CDP data plane, so we relay them up to the shell:
 *
 *   iframe → window.parent.postMessage → shell → SW → islandState.
 *
 * This is STRICTLY side-effect-only: it never mutates stream state and
 * any throw is swallowed so it can never break the chat. It is GUARDED on
 * `window.parent !== window` so the non-framed main-app sidebar — where the
 * same router runs — emits nothing.
 *
 * Status derivation:
 *   - `started` (no content yet)              → `thinking`
 *   - tool call running, name `browser_*`     → `{kind:'browser_tool', tool}`
 *   - tool call running, any other name       → `tool`
 *   - assistant content streaming             → `streaming`
 *   - `done` / complete                       → `ready`
 */
type IslandKind = 'thinking' | 'tool' | 'browser_tool' | 'streaming' | 'ready';

/** Extract a tool-call name from one wire tool_call (flat OR nested shape). */
function toolCallName(tc: unknown): string | null {
  if (!isObject(tc)) return null;
  if (typeof tc.name === 'string') return tc.name;
  if (isObject(tc.function) && typeof tc.function.name === 'string') {
    return tc.function.name;
  }
  return null;
}

/**
 * Post the island phase to the parent shell. No-op (and never throws) when not
 * framed, and swallows any postMessage failure — this must be invisible to the
 * stream.
 */
function emitIslandPhase(kind: IslandKind, tool?: string): void {
  try {
    if (typeof window === 'undefined') return;
    if (window.parent === window) return; // not framed → main-app sidebar
    window.parent.postMessage(
      tool !== undefined
        ? { type: 'ISLAND_PHASE', kind, tool }
        : { type: 'ISLAND_PHASE', kind },
      '*',
    );
  } catch {
    // Side-effect only: a failed relay must never surface into the chat.
  }
}

/**
 * Derive + emit the island phase for one CHAT_UPDATE chunk. A chunk that
 * announces tool calls drives the `tool` / `browser_tool` phase (browser tools
 * stripped of their `browser_` prefix); a plain assistant chunk with content
 * drives `streaming`. Chunks we can't classify (e.g. a bare tool RESULT) emit
 * nothing and leave the current island state as-is.
 */
function emitChunkPhase(chunk: StreamChunk): void {
  if (Array.isArray(chunk.tool_calls) && chunk.tool_calls.length > 0) {
    for (const tc of chunk.tool_calls) {
      const name = toolCallName(tc);
      if (name && name.startsWith('browser_')) {
        emitIslandPhase('browser_tool', name.replace(/^browser_/, ''));
        return;
      }
    }
    emitIslandPhase('tool');
    return;
  }
  if (chunk.role === 'assistant' && chunk.content !== '') {
    emitIslandPhase('streaming');
  }
}

function emitChatEventPhase(event: ChatStreamEvent): void {
  if (event.type === 'tool_start') {
    if (event.name.startsWith('browser_')) {
      emitIslandPhase('browser_tool', event.name.replace(/^browser_/, ''));
    } else {
      emitIslandPhase('tool');
    }
    return;
  }
  if (event.type === 'tool_update') {
    emitIslandPhase('tool');
    return;
  }
  if (
    (event.type === 'message_delta' && event.delta) ||
    (event.type === 'message_replace' && event.content)
  ) {
    emitIslandPhase('streaming');
  }
}

/** A NOTICE may accompany a running turn or terminate a rejected turn. Its
 * control-flow effect is explicit and independent from toast severity. */
interface NoticeFrame {
  level?: 'info' | 'warning' | 'error';
  code?: string;
  message: string;
  turnDisposition: 'continue' | 'cancel';
}

function getNotice(payload: unknown): NoticeFrame | null {
  if (!isObject(payload)) return null;
  if (typeof payload.message !== 'string' || !payload.message) return null;
  return {
    level:
      payload.level === 'warning' || payload.level === 'error'
        ? payload.level
        : 'info',
    code: typeof payload.code === 'string' ? payload.code : undefined,
    message: payload.message,
    turnDisposition: payload.turn_disposition === 'cancel' ? 'cancel' : 'continue',
  };
}

/**
 * Route one decoded SSE frame to the right side-effect.
 *
 * `client` is injected for testability; production code calls the
 * thin wrapper `routeAgentSignal` which uses the module-scope singleton.
 */
export function routeAgentSignalWith(
  client: QueryClient,
  event: string,
  payload: unknown,
  ctx: RouteSignalContext,
  presentation: RouteSignalPresentation = defaultPresentation,
): void {
  const store = useChatStreamStore.getState();

  switch (event) {
    case 'started': {
      // The composer already optimistically began the turn (user bubble +
      // thinking). Just record the real turnId + keep streaming — do NOT clear
      // the buffer (that would drop the optimistic user message).
      const turnId = getTurnId(payload);
      if (turnId) {
        store.markStarted(turnId, ctx.chatId);
        // Persist the in-flight turn so re-entry can RESUME it (replay + live).
        rememberActiveTurn({
          wfId: ctx.wfId,
          chatId: ctx.chatId,
          turnId,
        });
      }
      // Island (§14.1): a turn just began with no content yet → thinking.
      emitIslandPhase('thinking');
      return;
    }
    case 'CHAT_UPDATE': {
      const chunk = getChunk(payload);
      if (chunk) {
        store.appendChunk(chunk, ctx.chatId);
        if (chunk.role === 'assistant' && chunk.content.trim()) {
          store.setStartupProgress(null, ctx.chatId);
        }
        // Island (§14.1): derive tool / browser_tool / streaming from the chunk.
        emitChunkPhase(chunk);
      }
      return;
    }
    case 'CHAT_EVENT': {
      const event = getChatEvent(payload);
      if (event) {
        store.applyEvent(event, ctx.chatId);
        if (
          (event.type === 'message_delta' && event.delta.length > 0)
          || (event.type === 'message_replace' && event.content.trim().length > 0)
        ) {
          store.setStartupProgress(null, ctx.chatId);
        }
        if (event.type === 'todo_update') {
          client.invalidateQueries({
            queryKey: ['chat-state', ctx.wfId, ctx.chatId],
          });
        }
        if (event.type === 'tool_end') {
          // Runtime-native tool results (notably Codex ``shell`` and
          // ``file_change``) have no artifact envelope.  Their canonical
          // invocation still carries the tool name, so use it before the
          // legacy artifact metadata fallback.  The backend has already
          // completed VFS write-through before emitting this tool_end.
          const tool = event.invocation?.name
            || toolNameFromArtifact(event.artifact);
          if (tool === 'create_workflow' || tool === 'set_workflow') {
            const workflowId = workflowIdFromToolArtifact(event.artifact);
            client.invalidateQueries({ queryKey: ['chat-workspace', ctx.chatId] });
            client.invalidateQueries({ queryKey: ['project-sandboxes'] });
            client.invalidateQueries({ queryKey: ['vfs'] });
            client.invalidateQueries({ queryKey: ['storage'] });
            if (workflowId) {
              client.invalidateQueries({ queryKey: ['workflow', workflowId] });
            }
            client.invalidateQueries({ queryKey: ['chats', ctx.wfId] });
          }
          if (tool === 'update_canvas') {
            const workflowId = workflowIdFromToolArtifact(event.artifact);
            client.invalidateQueries({ queryKey: ['vfs'] });
            client.invalidateQueries({ queryKey: ['storage'] });
            client.invalidateQueries({ queryKey: ['workflow', workflowId ?? ctx.wfId] });
          }
          if (tool && VFS_MUTATING_TOOLS.has(tool)) {
            client.invalidateQueries({ queryKey: ['vfs'] });
            client.invalidateQueries({ queryKey: ['storage'] });
          }
        }
        emitChatEventPhase(event);
      }
      return;
    }
    case 'RUNTIME_STATUS': {
      if (!isObject(payload)) return;
      const phase = payload.phase;
      if (
        phase === 'preparing_environment'
        || phase === 'queueing'
        || phase === 'acquiring_sandbox'
        || phase === 'mounting_workspace'
        || phase === 'initializing_runtime'
        || phase === 'connecting_model'
        || phase === 'awaiting_first_output'
        || phase === 'running_tool'
        || phase === 'finalizing'
      ) {
        store.setStartupProgress({
          phase,
          startedAt: typeof payload.started_at === 'string'
            ? payload.started_at
            : new Date().toISOString(),
          firstTurn: payload.first_turn === true,
          runtimeType: typeof payload.runtime_type === 'string'
            ? payload.runtime_type
            : '',
          operationId: typeof payload.operation_id === 'string'
            ? payload.operation_id
            : undefined,
          label: typeof payload.label === 'string' && payload.label.trim()
            ? payload.label
            : undefined,
        }, ctx.chatId);
      }
      return;
    }
    case 'VIBE_ACTION': {
      // Backend has already committed the agent's edits to the version tree;
      // META_SYNC arrives shortly after, but we also invalidate here so the
      // canvas refetches immediately on the post-action state.
      invalidateWorkflow(client, payload, ctx.wfId);
      return;
    }
    case 'META_SYNC': {
      invalidateWorkflow(client, payload, ctx.wfId);
      return;
    }
    case 'WORKFLOW_SYNC': {
      // Emitted by the agent's `show_workflow` tool via _flush_pending_show.
      // Same body as META_SYNC / VIBE_ACTION — refetch the workflow.
      // (T12 may upgrade this to setQueryData with the payload contents.)
      invalidateWorkflow(client, payload, ctx.wfId);
      return;
    }
    case 'EXEC_UPDATE': {
      // Exec events flow through `streamExecution` (lib/api/sse/exec-stream.ts),
      // not this router — they arrive on a separate SSE endpoint
      // (`/executions`) and feed `useExecStreamStore`, not chat-stream.
      // The case stays as an explicit no-op so the `unknown event` warning
      // doesn't fire if an exec frame ever leaks onto the chat channel.
      return;
    }
    case 'HISTORY_SYNC': {
      client.invalidateQueries({
        queryKey: ['chat-history', ctx.wfId, ctx.chatId],
      });
      client.invalidateQueries({
        queryKey: ['chat-state', ctx.wfId, ctx.chatId],
      });
      return;
    }
    case 'HITL_RESOLVED': {
      store.setWaitingForUser(false, ctx.chatId);
      client.invalidateQueries({
        queryKey: ['chat-history', ctx.wfId, ctx.chatId],
      });
      client.invalidateQueries({
        queryKey: ['chats', ctx.wfId],
      });
      return;
    }
    case 'HITL_REQUIRED': {
      // Control-plane notification. The backend emits the durable, renderable
      // approval projection as a CHAT_EVENT immediately after this frame.
      // Keeping this explicit prevents a misleading "unknown event" warning
      // without applying the same approval state twice.
      store.setWaitingForUser(true, ctx.chatId);
      return;
    }
    case 'INTERACTION_REQUIRED': {
      // Same lifecycle as an approval, with a richer input artifact. Runtime
      // adapters may differ, but the frontend waiting state is portable.
      store.setWaitingForUser(true, ctx.chatId);
      return;
    }
    case 'INTERACTION_RESOLVED': {
      store.setWaitingForUser(false, ctx.chatId);
      client.invalidateQueries({
        queryKey: ['chat-history', ctx.wfId, ctx.chatId],
      });
      return;
    }
    case 'done': {
      const turnId = useChatStreamStore.getState().runtimes[ctx.chatId]?.turnId ?? null;
      store.setState('complete', ctx.chatId);
      clearActiveTurn({ wfId: ctx.wfId, chatId: ctx.chatId }); // turn finished → nothing to resume
      // Island (§14.1): turn finished → back to the idle/ready pill.
      emitIslandPhase('ready');
      client.invalidateQueries({
        queryKey: ['chat-history', ctx.wfId, ctx.chatId],
      });
      // Backend auto-creates a chat row on the first user message, so the
      // left-rail session list needs a refresh after the first `done`.
      client.invalidateQueries({ queryKey: ['chats', ctx.wfId] });
      handoffToDurableHistory(client, ctx, turnId, presentation);
      return;
    }
    case 'error': {
      const turnId = useChatStreamStore.getState().runtimes[ctx.chatId]?.turnId ?? null;
      const code = isObject(payload) && typeof payload.code === 'string'
        ? payload.code
        : '';
      store.setState(code === 'cancelled' ? 'cancelled' : 'failed', ctx.chatId);
      clearActiveTurn({ wfId: ctx.wfId, chatId: ctx.chatId }); // turn ended (cancelled/failed) → nothing to resume
      // Island (§14.1): a failed/cancelled turn must not leave the island stuck
      // mid-phase — drop back to ready (the island's visibility is gated by the
      // debugger lifecycle, not by this, so this only clears the content).
      emitIslandPhase('ready');
      if (code === 'cancelled') {
        client.invalidateQueries({
          queryKey: ['chat-history', ctx.wfId, ctx.chatId],
        });
        client.invalidateQueries({
          queryKey: ['chat-state', ctx.wfId, ctx.chatId],
        });
        client.invalidateQueries({ queryKey: ['chats', ctx.wfId] });
      }
      handoffToDurableHistory(client, ctx, turnId, presentation);
      return;
    }
    case 'NOTICE': {
      // Presentation is the default. Only a backend-declared cancellation
      // releases the optimistic projection; runtime warnings must preserve the
      // active turn and its subsequent token stream.
      const notice = getNotice(payload);
      if (!notice) return;
      // Localize by the backend `code` (a stable key), falling back to the
      // backend-provided `message` for an unknown/absent code.
      const text = notice.code
        ? i18n.t(`notice.${notice.code}`, { defaultValue: notice.message })
        : notice.message;
      presentation.showNotice(notice.level ?? 'info', text);
      if (notice.turnDisposition === 'cancel') {
        store.resetRuntime(ctx.chatId);
      }
      return;
    }
    case 'NO_OP': {
      // Backend emits NO_OP when a turn yielded no UI-actionable signal
      // (e.g. agent finished without tool calls or vibe). The `done`
      // frame that follows drives the lifecycle to complete; nothing to
      // do here. Explicit case prevents the unknown-event warn from
      // firing on every idle turn.
      return;
    }
    case 'HEARTBEAT': {
      // Transport keepalive for long-running tools. It carries no semantic
      // state and must not affect rendering.
      return;
    }
    case 'USAGE': {
      // Provider token accounting is a valid terminal-adjacent telemetry
      // frame. Usage is persisted by the backend; Chat presentation does not
      // need to mutate, and treating it as unknown only pollutes the browser
      // console during otherwise successful Turns.
      return;
    }
    default: {
      console.warn('[sse] unknown event', event, payload);
    }
  }
}

/** Production entry point — binds to the app's singleton QueryClient. */
export function routeAgentSignal(
  event: string,
  payload: unknown,
  ctx: RouteSignalContext,
): void {
  routeAgentSignalWith(queryClient, event, payload, ctx, {
    ...defaultPresentation,
    loadDurableHistory: fetchChatHistory,
  });
}
