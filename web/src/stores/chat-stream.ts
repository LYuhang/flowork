/**
 * Chat-stream store — holds in-flight agent turns.
 *
 * One agent turn is the round-trip from the user pressing Send to the
 * backend emitting a terminal event. While the turn is alive, the immutable
 * pre-Turn checkpoint is the transcript base and this store owns the live tail.
 * On termination, the SSE router loads canonical database history, installs it
 * in React Query, and only then calls `finishProjection`; ownership therefore
 * changes without content matching, duplicate bubbles, or a blank render seam.
 *
 * Design choices, with the legacy `ChatManager` in mind:
 *   - Live turns are keyed by `chatId`. A user may switch to another chat
 *     while an agent is still streaming; that background chat must keep its
 *     live transcript/tool state warm so switching back is instant.
 *   - `abortController` is stored so the Stop button (and Esc cascade,
 *     T17) can fire `.abort()` without prop-drilling. The fetch handler
 *     in `agent-stream.ts` sets it; the composer clears it on completion.
 *   - `state` is a 5-value enum matching the plan's lifecycle so future
 *     UI affordances (Retry on `failed`, banner on `interrupted`) can
 *     branch off the same source of truth.
 *
 * `subscribeWithSelector` mirrors `auth.ts` / `workflow-edit.ts` and lets
 * the route-signal layer call `useChatStreamStore.getState()` from outside
 * React without paying the cost of a hook subscription.
 */
import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import type { components } from '@/lib/api/schema';
import type { ApprovalMode } from '@/stores/agent-settings';
import type { ToolInvocationEnvelope } from '@/components/agent-sidebar/types';

type Attachment = NonNullable<components['schemas']['MessagePostBody']['attachments']>[number];

/** Completed/inactive turns stay warm for fast chat switching, but the
 * browser must not retain every transcript touched during a long session.
 * Live turns are always protected and do not count toward this budget. */
export const MAX_INACTIVE_CHAT_RUNTIMES = 20;
const MAX_COMPOSER_INPUTS = 50;
const MAX_PENDING_ATTACHMENT_CHATS = 20;

export type StreamState =
  | 'idle'
  | 'streaming'
  | 'complete'
  | 'cancelled'
  | 'interrupted'
  | 'failed';

export type RuntimeStartupPhase =
  | 'preparing_environment'
  | 'queueing'
  | 'acquiring_sandbox'
  | 'mounting_workspace'
  | 'initializing_runtime'
  | 'connecting_model'
  | 'awaiting_first_output'
  | 'running_tool'
  | 'finalizing';

export interface RuntimeStartupProgress {
  phase: RuntimeStartupPhase;
  startedAt: string;
  firstTurn: boolean;
  runtimeType: string;
  operationId?: string;
  label?: string;
}

/**
 * Minimal shape of a streamed chat chunk. Matches the persisted
 * `HistoryMessage` schema's load-bearing fields plus `tool_call_id` which
 * is emitted on `role: 'tool'` frames but not stored in history page-outs.
 */
export interface StreamChunk {
  role: string;
  content: string;
  attachments?: Attachment[];
  tool_calls?: unknown[];
  tool_call_id?: string;
  artifact?: Record<string, unknown> | null;
  invocation?: ToolInvocationEnvelope;
}

export interface StreamToolCall {
  id: string;
  name: string;
  arguments: string;
  result?: string;
  artifact?: Record<string, unknown> | null;
  status: 'running' | 'done' | 'error';
  invocation?: ToolInvocationEnvelope;
}

export interface StreamUiMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  attachments?: Attachment[];
  tool_calls: StreamToolCall[];
  artifact?: Record<string, unknown> | null;
  /**
   * Open extension slot for future rich UI. Tools can expose artifact-defined
   * buttons/forms/cards without changing the message lifecycle protocol.
   */
  parts?: Array<Record<string, unknown>>;
}

export interface TodoItem {
  id: number;
  text: string;
  status: 'pending' | 'in_progress' | 'done';
}

export interface ComposerDraft {
  chatId: string | null;
  text: string;
}

export interface ChatRuntime {
  turnId: string | null;
  chatId: string;
  /** True while the live Turn projection owns the transcript tail. A terminal
   * history refresh atomically hands ownership back to the durable transcript. */
  projectionActive: boolean;
  state: StreamState;
  /** The Turn remains active, but progress is paused for an approval/input
   * response. This is a frontend projection of backend run/HITL state, not a
   * separately persisted source of truth. */
  waitingForUser: boolean;
  buffer: StreamChunk[];
  messages: StreamUiMessage[];
  todoItems: TodoItem[] | null;
  abortController: AbortController | null;
  lastInput: LastInput | null;
  startupPhase: RuntimeStartupPhase | null;
  startupProgress: RuntimeStartupProgress | null;
}

export type ChatStreamEvent =
  | { type: 'message_start'; message_id: string; role: 'assistant'; content?: string; artifact?: Record<string, unknown> | null; parts?: Array<Record<string, unknown>> }
  | { type: 'message_delta'; message_id: string; delta: string }
  | { type: 'message_replace'; message_id: string; content: string; artifact?: Record<string, unknown> | null; parts?: Array<Record<string, unknown>> }
  | { type: 'message_end'; message_id: string }
  | { type: 'tool_start'; message_id: string; tool_call_id: string; name: string; arguments?: string; invocation?: ToolInvocationEnvelope; artifact?: Record<string, unknown> | null; parts?: Array<Record<string, unknown>> }
  | { type: 'tool_delta'; tool_call_id: string; arguments_delta?: string }
  | { type: 'tool_update'; tool_call_id: string; content?: string; artifact?: Record<string, unknown> | null; status?: 'running' | 'done' | 'error' }
  | { type: 'tool_end'; tool_call_id: string; content: string; invocation?: ToolInvocationEnvelope; artifact?: Record<string, unknown> | null; status?: 'done' | 'error' }
  | { type: 'todo_update'; items: TodoItem[] };

/**
 * What we need to resend a turn when the user clicks Retry.
 *
 * Stored at send-time inside the composer. The Retry button reads from
 * here and re-fires `streamAgentTurn` with the same content/attachments,
 * which is the cheapest way to recover from `failed` / `interrupted` —
 * no scrolling back through history, no re-typing.
 */
export interface LastInput {
  skillUse?: import('@/lib/api/sse/agent-stream').SkillUseSelection;
  content: string;
  projectId?: string | null;
  control?: import('@/lib/api/sse/agent-stream').HitlContinueControl;
  attachments?: Attachment[];
  /** Preserved so a retried `/browser` send keeps the same routing mode. */
  mode?: 'chat' | 'browser';
  /** Frontend/product surfaces are part of the backend protocol gate. */
  surface?: 'main' | 'sidepanel';
  agentSurface?: 'chat' | 'browser';
  /** Per-turn authorization policy captured at send-time. */
  approvalMode?: ApprovalMode;
}

/**
 * `pendingAttachments` (T15.5) is a transient buffer of context chips the
 * user has built up *before* pressing Send. The composer renders the chips
 * above the textarea, and `doSend` drains them into the outgoing
 * `streamAgentTurn` payload — at which point we `clearAttachments()` so the
 * next turn starts fresh.
 *
 * We intentionally do *not* persist this across `beginTurn`: a new turn
 * always starts with whatever the user has explicitly attached for that
 * turn. `reset()` and `clearAttachments()` both wipe it.
 *
 * Why the same `Attachment` type as `LastInput.attachments`: the chips,
 * the persisted retry payload, and the outgoing fetch body all need to
 * agree on shape, and the OpenAPI `Attachment` schema is the canonical
 * source.
 */
export interface ChatStreamState {
  runtimes: Record<string, ChatRuntime>;
  turnId: string | null;
  chatId: string | null;
  state: StreamState;
  waitingForUser: boolean;
  buffer: StreamChunk[];
  messages: StreamUiMessage[];
  todoItems: TodoItem[] | null;
  /** Legacy slots retained for old callers; the Chat UI no longer renders them. */
  carry: StreamChunk[];
  carryMessages: StreamUiMessage[];
  abortController: AbortController | null;
  lastInput: LastInput | null;
  startupPhase: RuntimeStartupPhase | null;
  startupProgress: RuntimeStartupProgress | null;
  pendingAttachments: Record<string, Attachment[]>;
  composerInputs: Record<string, string>;
  /**
   * Pending composer draft text to PREFILL into the textarea (F2). A tool
   * `ErrorCard`'s "Ask the agent to fix this" writes a follow-up message here;
   * `ChatComposer` consumes it (effect → setValue, then clears via
   * `consumeDraft`). `null` = nothing to prefill. We round-trip through the
   * store rather than prop-drilling a setter down
   * ChatMessageList → MessageItem → ToolCallBlock → ErrorCard.
   */
  draft: ComposerDraft | null;
  beginTurn: (chatId: string, turnId: string) => void;
  markStarted: (turnId: string, chatId?: string) => void;
  appendChunk: (chunk: StreamChunk, chatId?: string) => void;
  applyEvent: (event: ChatStreamEvent, chatId?: string) => void;
  setTodoItems: (items: TodoItem[] | null, chatId?: string) => void;
  setState: (s: StreamState, chatId?: string) => void;
  setWaitingForUser: (waiting: boolean, chatId?: string) => void;
  setAbort: (a: AbortController | null, chatId?: string) => void;
  setLastInput: (input: LastInput | null, chatId?: string) => void;
  setStartupPhase: (phase: RuntimeStartupPhase | null, chatId?: string) => void;
  setStartupProgress: (progress: RuntimeStartupProgress | null, chatId?: string) => void;
  /** Queue a draft for the composer to prefill. */
  setDraft: (text: string, chatId?: string | null) => void;
  /** Read-and-clear the queued draft (composer calls this once applied). */
  consumeDraft: () => void;
  setComposerInput: (chatId: string, value: string) => void;
  addAttachment: (chatId: string, a: Attachment) => void;
  setAttachments: (chatId: string, attachments: Attachment[]) => void;
  removeAttachmentAt: (chatId: string, idx: number) => void;
  clearAttachments: (chatId: string) => void;
  reset: () => void;
  resetRuntime: (chatId: string) => void;
  finishProjection: (chatId: string, turnId: string | null) => void;
}

function emptyRuntime(chatId: string): ChatRuntime {
  return {
    chatId,
    turnId: null,
    projectionActive: false,
    state: 'idle',
    waitingForUser: false,
    buffer: [],
    messages: [],
    todoItems: null,
    abortController: null,
    lastInput: null,
    startupPhase: null,
    startupProgress: null,
  };
}

function runtimeFor(state: ChatStreamState, chatId: string): ChatRuntime {
  return state.runtimes[chatId] ?? emptyRuntime(chatId);
}

function retainRuntime(
  runtimes: Record<string, ChatRuntime>,
  chatId: string,
  runtime: ChatRuntime,
): Record<string, ChatRuntime> {
  const touched = { ...runtimes };
  // Object insertion order is our small LRU: move the touched runtime last.
  delete touched[chatId];
  touched[chatId] = runtime;
  const entries = Object.entries(touched);
  const protectedEntries = entries.filter(
    ([, item]) => item.state === 'streaming' || item.abortController !== null,
  );
  const inactiveEntries = entries.filter(
    ([, item]) => item.state !== 'streaming' && item.abortController === null,
  );
  return Object.fromEntries([
    ...protectedEntries,
    ...inactiveEntries.slice(-MAX_INACTIVE_CHAT_RUNTIMES),
  ]);
}

function retainRecordValue<T>(
  record: Record<string, T>,
  key: string,
  value: T,
  limit: number,
): Record<string, T> {
  const next = { ...record };
  delete next[key];
  next[key] = value;
  return Object.fromEntries(Object.entries(next).slice(-limit));
}

function legacyPatchFromRuntime(runtime: ChatRuntime): Pick<
  ChatStreamState,
  | 'chatId'
  | 'turnId'
  | 'state'
  | 'waitingForUser'
  | 'buffer'
  | 'messages'
  | 'todoItems'
  | 'abortController'
  | 'lastInput'
  | 'startupPhase'
  | 'startupProgress'
> {
  return {
    chatId: runtime.chatId,
    turnId: runtime.turnId,
    state: runtime.state,
    waitingForUser: runtime.waitingForUser,
    buffer: runtime.buffer,
    messages: runtime.messages,
    todoItems: runtime.todoItems,
    abortController: runtime.abortController,
    lastInput: runtime.lastInput,
    startupPhase: runtime.startupPhase,
    startupProgress: runtime.startupProgress,
  };
}

function toolStatusFromArtifact(artifact?: Record<string, unknown> | null): StreamToolCall['status'] {
  const status = typeof artifact?.status === 'string' ? artifact.status : '';
  return status === 'error' || status === 'cancelled' || status === 'canceled'
    ? 'error'
    : 'done';
}

function sanitizeVisibleContent(content: string): string {
  return content || '';
}

function readToolCall(raw: unknown): { id: string; name: string; arguments: string; invocation?: ToolInvocationEnvelope } | null {
  if (!raw || typeof raw !== 'object') return null;
  const obj = raw as Record<string, unknown>;
  const fn = obj.function && typeof obj.function === 'object'
    ? obj.function as Record<string, unknown>
    : null;
  const id = typeof obj.id === 'string' && obj.id ? obj.id : null;
  if (!id) return null;
  const name =
    (typeof obj.name === 'string' && obj.name) ||
    (typeof fn?.name === 'string' && fn.name) ||
    '(unknown tool)';
  const args = 'arguments' in (fn ?? {})
    ? fn?.arguments
    : obj.arguments;
  return {
    id,
    name,
    arguments: typeof args === 'string' ? args : JSON.stringify(args ?? {}),
    invocation: obj.invocation && typeof obj.invocation === 'object'
      ? obj.invocation as ToolInvocationEnvelope
      : undefined,
  };
}

function upsertMessage(
  messages: StreamUiMessage[],
  message: StreamUiMessage,
): StreamUiMessage[] {
  const idx = messages.findIndex((m) => m.id === message.id);
  if (idx < 0) return [...messages, message];
  const next = messages.slice();
  next[idx] = { ...next[idx], ...message };
  return next;
}

function applyToolEndToMessages(
  messages: StreamUiMessage[],
  event: Extract<ChatStreamEvent, { type: 'tool_end' }>,
): StreamUiMessage[] {
  let matched = false;
  const byId = messages.map((m) => ({
    ...m,
    tool_calls: m.tool_calls.map((call) => {
      if (call.id !== event.tool_call_id) return call;
      matched = true;
      return {
        ...call,
        result: event.content,
        // A long-running tool may publish its interactive artifact in an
        // earlier tool_update. Terminal frames can omit unchanged payloads,
        // so preserve the last projection instead of hiding the form exactly
        // when the Runtime resumes.
        artifact: event.artifact ?? call.artifact ?? null,
        status: event.status ?? toolStatusFromArtifact(event.artifact),
        invocation: event.invocation ?? call.invocation,
      };
    }),
  }));
  if (matched) return byId;
  return messages;
}

function applyStreamEvent(
  messages: StreamUiMessage[],
  event: ChatStreamEvent,
): StreamUiMessage[] {
  switch (event.type) {
    case 'message_start':
      return upsertMessage(messages, {
        id: event.message_id,
        role: event.role,
        content: sanitizeVisibleContent(event.content ?? ''),
        tool_calls: messages.find((m) => m.id === event.message_id)?.tool_calls ?? [],
        artifact: event.artifact ?? undefined,
        parts: event.parts,
      });
    case 'message_delta':
      return messages.map((m) =>
        m.id === event.message_id
          ? (() => {
              const content = sanitizeVisibleContent(m.content + event.delta);
              return { ...m, content };
            })()
          : m,
      ).filter((m): m is StreamUiMessage => Boolean(m));
    case 'message_replace':
      return upsertMessage(messages, {
        id: event.message_id,
        role: messages.find((m) => m.id === event.message_id)?.role ?? 'assistant',
        content: sanitizeVisibleContent(event.content),
        tool_calls: messages.find((m) => m.id === event.message_id)?.tool_calls ?? [],
        artifact: event.artifact ?? messages.find((m) => m.id === event.message_id)?.artifact,
        parts: event.parts ?? messages.find((m) => m.id === event.message_id)?.parts,
      });
    case 'message_end':
      return messages;
    case 'tool_start': {
      const existing = messages.find((m) => m.id === event.message_id);
      const base: StreamUiMessage = existing ?? {
        id: event.message_id,
        role: 'assistant',
        content: '',
        tool_calls: [],
      };
      const tool: StreamToolCall = {
        id: event.tool_call_id,
        name: event.name,
        arguments: event.arguments ?? '',
        artifact: event.artifact ?? null,
        status: 'running',
        invocation: event.invocation,
      };
      const toolIdx = base.tool_calls.findIndex((call) => call.id === tool.id);
      const toolCalls = base.tool_calls.slice();
      if (toolIdx >= 0) toolCalls[toolIdx] = { ...toolCalls[toolIdx], ...tool };
      else toolCalls.push(tool);
      return upsertMessage(messages, {
        ...base,
        tool_calls: toolCalls,
        parts: event.parts ?? base.parts,
      });
    }
    case 'tool_delta':
      return messages.map((m) => ({
        ...m,
        tool_calls: m.tool_calls.map((call) =>
          call.id === event.tool_call_id
            ? { ...call, arguments: call.arguments + (event.arguments_delta ?? '') }
            : call,
        ),
      }));
    case 'tool_update':
      return messages.map((m) => ({
        ...m,
        tool_calls: m.tool_calls.map((call) =>
          call.id === event.tool_call_id
            ? {
                ...call,
                // Runtime progress (shell output, patch deltas, MCP progress,
                // reasoning summaries) is part of the live projection, not a
                // control-only heartbeat.
                result: typeof event.content === 'string'
                  ? `${call.result ?? ''}${event.content}`
                  : call.result,
                artifact: event.artifact ?? call.artifact ?? null,
                status: event.status ?? call.status,
              }
            : call,
        ),
      }));
    case 'tool_end':
      return applyToolEndToMessages(messages, event);
    default:
      return messages;
  }
}

function applyLegacyChunk(messages: StreamUiMessage[], chunk: StreamChunk): StreamUiMessage[] {
  if (chunk.role === 'tool' && chunk.tool_call_id) {
    const status = toolStatusFromArtifact(chunk.artifact) === 'error' ? 'error' : 'done';
    return applyStreamEvent(messages, {
      type: 'tool_end',
      tool_call_id: chunk.tool_call_id,
      content: chunk.content,
      artifact: chunk.artifact,
      invocation: chunk.invocation,
      status,
    });
  }
  if (chunk.role === 'assistant' && Array.isArray(chunk.tool_calls) && chunk.tool_calls.length > 0) {
    let next = messages;
    const messageId = `toolmsg:${chunk.tool_calls.map((call) => readToolCall(call)?.id ?? '').join(':')}`;
    next = applyStreamEvent(next, {
      type: 'message_start',
      message_id: messageId,
      role: 'assistant',
      content: '',
    });
    for (const raw of chunk.tool_calls) {
      const call = readToolCall(raw);
      if (!call) continue;
      next = applyStreamEvent(next, {
        type: 'tool_start',
        message_id: messageId,
        tool_call_id: call.id,
        name: call.name,
        arguments: call.arguments,
        invocation: call.invocation,
      });
    }
    return next;
  }
  if (chunk.role === 'assistant') {
    const last = messages[messages.length - 1];
    const lastIsPlainAssistant =
      last?.role === 'assistant' &&
      (!Array.isArray(last.tool_calls) || last.tool_calls.length === 0);
    const messageId = lastIsPlainAssistant
      ? last.id
      : `assistant:stream:${messages.length}`;
    return applyStreamEvent(messages, {
      type: 'message_replace',
      message_id: messageId,
      content: sanitizeVisibleContent(chunk.content),
    });
  }
  if (chunk.role === 'user') {
    return upsertMessage(messages, {
      id: `${chunk.role}:${messages.length}:${chunk.content}`,
      role: chunk.role,
      content: chunk.content,
      attachments: chunk.attachments,
      tool_calls: [],
      artifact: chunk.artifact,
    });
  }
  return messages;
}

function legacyChunksForEvent(event: ChatStreamEvent): StreamChunk[] {
  if (event.type === 'message_replace') {
    return [{ role: 'assistant', content: event.content }];
  }
  if (event.type === 'tool_start') {
    return [{
      role: 'assistant',
      content: '',
      tool_calls: [{
        id: event.tool_call_id,
        name: event.name,
        arguments: event.arguments ?? '',
        invocation: event.invocation,
      }],
    }];
  }
  if (event.type === 'tool_end') {
    return [{
      role: 'tool',
      content: event.content,
      tool_call_id: event.tool_call_id,
      artifact: event.artifact,
      invocation: event.invocation,
    }];
  }
  return [];
}

function appendLegacyBuffer(buffer: StreamChunk[], chunks: StreamChunk[]): StreamChunk[] {
  let next = buffer;
  for (const chunk of chunks) {
    const last = next[next.length - 1];
    const plainAssistant = (c?: StreamChunk) =>
      !!c &&
      c.role === 'assistant' &&
      !(Array.isArray(c.tool_calls) && c.tool_calls.length > 0);
    if (plainAssistant(chunk) && plainAssistant(last)) {
      next = [...next.slice(0, -1), chunk];
    } else {
      next = [...next, chunk];
    }
  }
  return next;
}

export const useChatStreamStore = create<ChatStreamState>()(
  subscribeWithSelector((set) => ({
    runtimes: {},
    turnId: null,
    chatId: null,
    state: 'idle',
    waitingForUser: false,
    buffer: [],
    messages: [],
    todoItems: null,
    carry: [],
    carryMessages: [],
    abortController: null,
    lastInput: null,
    startupPhase: null,
    startupProgress: null,
    pendingAttachments: {},
    composerInputs: {},
    draft: null,

    beginTurn: (chatId, turnId) =>
      set((s) => {
        const prev = runtimeFor(s, chatId);
        const runtime: ChatRuntime = {
          ...prev,
          chatId,
          turnId,
          projectionActive: true,
          state: 'streaming',
          waitingForUser: false,
          buffer: [],
          messages: [],
          todoItems: null,
          startupPhase: null,
          startupProgress: null,
        };
        return {
          runtimes: retainRuntime(s.runtimes, chatId, runtime),
          carry: [],
          carryMessages: [],
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    // The real backend `started` frame arrives AFTER the composer has already
    // optimistically begun the turn (user bubble + thinking). So `started` only
    // records the turnId + (re)asserts streaming — it must NOT clear the buffer,
    // or it would wipe the optimistic user message until the turn persists.
    markStarted: (turnId, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return { turnId, state: 'streaming' };
        const runtime: ChatRuntime = {
          ...runtimeFor(s, targetChatId),
          turnId,
          projectionActive: true,
          state: 'streaming',
          waitingForUser: false,
        };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    // Streaming assistant text frames are CUMULATIVE — the backend
    // (`agent.py` `streaming_text += delta`) re-sends the FULL text-so-far on
    // every token (for example, "I" → "I am" → "I am ready"). Naively appending each frame
    // makes `mergeChunks` emit one bubble per token. So when the incoming
    // frame is plain assistant text (no tool calls) AND the last buffered
    // frame is the same, REPLACE it in place — the cumulative content folds
    // into one growing bubble. Anything else (user msg, an assistant frame
    // that announces tool_calls, a tool result) appends as a new entry, which
    // is exactly the boundary that starts a fresh assistant segment after a
    // tool round (the backend resets `streaming_text` to "" there).
    appendChunk: (chunk, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return {};
        const current = runtimeFor(s, targetChatId);
        const nextMessages = applyLegacyChunk(current.messages, chunk);
        const isPlainAssistant = (c?: StreamChunk) =>
          !!c &&
          c.role === 'assistant' &&
          !(Array.isArray(c.tool_calls) && c.tool_calls.length > 0);
        const last = current.buffer[current.buffer.length - 1];
        const sameChunk = (a?: StreamChunk, b?: StreamChunk) => {
          if (!a || !b) return false;
          if (a.role !== b.role || a.content !== b.content) return false;
          if ((a.tool_call_id ?? '') !== (b.tool_call_id ?? '')) return false;
          return (
            JSON.stringify(a.attachments ?? null) === JSON.stringify(b.attachments ?? null) &&
            JSON.stringify(a.tool_calls ?? null) === JSON.stringify(b.tool_calls ?? null) &&
            JSON.stringify(a.artifact ?? null) === JSON.stringify(b.artifact ?? null)
          );
        };
        let runtime: ChatRuntime;
        if (sameChunk(last, chunk)) {
          runtime = { ...current, messages: nextMessages };
        } else if (isPlainAssistant(chunk) && isPlainAssistant(last)) {
          runtime = {
            ...current,
            buffer: [...current.buffer.slice(0, -1), chunk],
            messages: nextMessages,
          };
        } else {
          runtime = {
            ...current,
            buffer: [...current.buffer, chunk],
            messages: nextMessages,
          };
        }
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    applyEvent: (event, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return {};
        const current = runtimeFor(s, targetChatId);
        let runtime: ChatRuntime;
        if (event.type === 'todo_update') {
          runtime = { ...current, todoItems: event.items };
        } else {
          runtime = {
            ...current,
            messages: applyStreamEvent(current.messages, event),
            buffer: appendLegacyBuffer(current.buffer, legacyChunksForEvent(event)),
          };
        }
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setTodoItems: (todoItems, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return { todoItems };
        const runtime = { ...runtimeFor(s, targetChatId), todoItems };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setState: (state, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return { state };
        const runtime = {
          ...runtimeFor(s, targetChatId),
          state,
          ...(state === 'streaming' ? {} : { startupPhase: null, startupProgress: null }),
          ...(state === 'streaming' ? {} : { waitingForUser: false }),
        };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setWaitingForUser: (waitingForUser, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return { waitingForUser };
        const runtime = { ...runtimeFor(s, targetChatId), waitingForUser };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setAbort: (abortController, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return { abortController };
        const runtime = { ...runtimeFor(s, targetChatId), abortController };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setLastInput: (lastInput, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        if (!targetChatId) return { lastInput };
        const runtime = { ...runtimeFor(s, targetChatId), lastInput };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setStartupPhase: (startupPhase, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        const startupProgress = startupPhase
          ? {
              phase: startupPhase,
              startedAt: new Date().toISOString(),
              firstTurn: false,
              runtimeType: '',
            }
          : null;
        if (!targetChatId) return { startupPhase, startupProgress };
        const runtime = { ...runtimeFor(s, targetChatId), startupPhase, startupProgress };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setStartupProgress: (startupProgress, chatId) =>
      set((s) => {
        const targetChatId = chatId ?? s.chatId;
        const startupPhase = startupProgress?.phase ?? null;
        if (!targetChatId) return { startupPhase, startupProgress };
        const runtime = {
          ...runtimeFor(s, targetChatId),
          startupPhase,
          startupProgress,
        };
        return {
          runtimes: retainRuntime(s.runtimes, targetChatId, runtime),
          ...legacyPatchFromRuntime(runtime),
        };
      }),

    setComposerInput: (chatId, value) =>
      set((s) => {
        if (!value) {
          const next = { ...s.composerInputs };
          delete next[chatId];
          return { composerInputs: next };
        }
        return {
          composerInputs: retainRecordValue(
            s.composerInputs,
            chatId,
            value,
            MAX_COMPOSER_INPUTS,
          ),
        };
      }),

    addAttachment: (chatId, attachment) =>
      set((s) => ({
        pendingAttachments: retainRecordValue(
          s.pendingAttachments,
          chatId,
          [...(s.pendingAttachments[chatId] ?? []), attachment],
          MAX_PENDING_ATTACHMENT_CHATS,
        ),
      })),

    setAttachments: (chatId, attachments) =>
      set((s) => {
        if (attachments.length === 0) {
          const next = { ...s.pendingAttachments };
          delete next[chatId];
          return { pendingAttachments: next };
        }
        return {
          pendingAttachments: retainRecordValue(
            s.pendingAttachments,
            chatId,
            attachments,
            MAX_PENDING_ATTACHMENT_CHATS,
          ),
        };
      }),

    removeAttachmentAt: (chatId, idx) =>
      set((s) => ({
        pendingAttachments: {
          ...s.pendingAttachments,
          [chatId]: (s.pendingAttachments[chatId] ?? []).filter((_, i) => i !== idx),
        },
      })),

    clearAttachments: (chatId) =>
      set((s) => {
        const next = { ...s.pendingAttachments };
        delete next[chatId];
        return { pendingAttachments: next };
      }),

    setDraft: (text, chatId = null) => set({ draft: { chatId, text } }),

    consumeDraft: () => set({ draft: null }),

    reset: () =>
      set({
        runtimes: {},
        turnId: null,
        chatId: null,
        state: 'idle',
        waitingForUser: false,
        buffer: [],
        messages: [],
        todoItems: null,
        carry: [],
        carryMessages: [],
        abortController: null,
        lastInput: null,
        startupPhase: null,
        startupProgress: null,
        pendingAttachments: {},
        composerInputs: {},
        draft: null,
      }),
    resetRuntime: (chatId) =>
      set((s) => {
        const next = { ...s.runtimes };
        delete next[chatId];
        if (s.chatId !== chatId) return { runtimes: next };
        return {
          runtimes: next,
          turnId: null,
          chatId: null,
          state: 'idle',
          waitingForUser: false,
          buffer: [],
          messages: [],
          todoItems: null,
          carry: [],
          carryMessages: [],
          abortController: null,
          lastInput: null,
          startupPhase: null,
          startupProgress: null,
        };
      }),
    finishProjection: (chatId, turnId) =>
      set((s) => {
        const current = s.runtimes[chatId];
        if (!current || current.turnId !== turnId) return {};
        const runtime: ChatRuntime = {
          ...current,
          projectionActive: false,
          buffer: [],
          messages: [],
        };
        return {
          runtimes: retainRuntime(s.runtimes, chatId, runtime),
          ...(s.chatId === chatId ? legacyPatchFromRuntime(runtime) : {}),
        };
      }),
  })),
);
