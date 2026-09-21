/**
 * Shared chat-rendering types + the `mergeChunks` reducer.
 *
 * The agent SSE stream emits chunks in a wire shape that is convenient for
 * the backend (one assistant message announcing tool calls, then one or
 * more `role: 'tool'` chunks delivering each result) but inconvenient for
 * the UI: a tool call and its result should render together as a single
 * collapsible block, not as two separate rows.
 *
 * `mergeChunks` folds the latter into the former. It is intentionally pure
 * and free of React, so the same reducer can run over:
 *   - the persisted history (`useChatHistory().data.items`), and
 *   - the in-flight `useChatStreamStore.buffer`,
 * before either reaches the renderer. That keeps the two data sources
 * presentation-equivalent — there's no "streaming vs history" branch in
 * the message component itself.
 *
 * The output shape (`MergedMessage`) deliberately drops the `'tool'` role
 * since every well-formed `'tool'` chunk has been absorbed into the
 * preceding assistant message's `tool_calls[i].result`. Orphan tool chunks
 * (e.g., a malformed stream) are skipped rather than emitted as their own
 * row so the renderer never has to handle the `'tool'` role.
 *
 * The reducer accepts either a nested tool-call shape
 * (`{id, type:'function', function:{name, arguments}}`) or a flat
 * `{id, name, arguments}` — both forms appear in practice depending on
 * which agent emitted the frame.
 */

/** Raw tool-call shape observed on the wire (both nested + flat). */
import type { components } from '@/lib/api/schema';

type Attachment = components['schemas']['Attachment'];

interface RawToolCall {
  id?: string;
  name?: string;
  arguments?: unknown;
  function?: {
    name?: string;
    arguments?: unknown;
  };
  invocation?: ToolInvocationEnvelope;
}

export interface ToolInvocationEnvelope {
  schemaVersion: 1;
  invocationId: string;
  runtime: { type: string; version?: string };
  /** Runtime-owned discriminator for optional specialized presenters. It must
   * never drive a second transport or recovery state machine. */
  nativeKind?: string;
  origin: string | {
    kind: 'builtin' | 'runtime_native' | 'platform_mcp' | 'custom_mcp' | 'dynamic' | 'unknown';
    provider?: string;
    serverId?: string | null;
    serverName?: string;
    serverLabel?: string;
    toolName?: string;
    qualifiedName?: string;
  };
  capability: string;
  name: string;
  status: 'queued' | 'running' | 'success' | 'error' | 'cancelled';
  input: unknown;
  risk?: 'read' | 'write' | 'execute' | 'external_side_effect' | 'unknown';
  output?: {
    content?: Array<Record<string, unknown>>;
    structuredContent?: unknown;
    isError?: boolean;
  };
  presentation?: { kind?: string; contentType?: string };
  timing?: { startedAt?: string; endedAt?: string; durationMs?: number };
  error?: { code?: string; message: string; retryable?: boolean };
}

/** Type guard: value is a non-null object we can probe with `in`. */
function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null;
}

/** Best-effort: coerce a wire-shape `arguments` field to a string. */
function argsToString(args: unknown): string {
  if (typeof args === 'string') return args;
  if (args === undefined || args === null) return '';
  try {
    return JSON.stringify(args);
  } catch {
    return String(args);
  }
}

/** Narrow a raw tool-call entry, tolerating both nested and flat shapes. */
function readRawToolCall(raw: unknown): RawToolCall | null {
  if (!isObject(raw)) return null;
  const out: RawToolCall = {};
  if (typeof raw.id === 'string') out.id = raw.id;
  if (typeof raw.name === 'string') out.name = raw.name;
  if ('arguments' in raw) out.arguments = raw.arguments;
  if (isObject(raw.invocation) && raw.invocation.schemaVersion === 1) {
    out.invocation = raw.invocation as unknown as ToolInvocationEnvelope;
  }
  if (isObject(raw.function)) {
    out.function = {};
    if (typeof raw.function.name === 'string') out.function.name = raw.function.name;
    if ('arguments' in raw.function) out.function.arguments = raw.function.arguments;
  }
  return out;
}

export interface MergedToolCall {
  id: string;
  name: string;
  arguments: string;
  result?: string;
  artifact?: Record<string, unknown> | null;
  status: 'running' | 'done' | 'error';
  invocation?: ToolInvocationEnvelope;
}

export interface MergedMessage {
  id?: string | null;
  role: 'user' | 'assistant' | 'system';
  content: string;
  attachments?: Attachment[];
  tool_calls: MergedToolCall[];
  ts?: number;
  activity?: {
    type?: string;
    delivery_batch_id?: string;
    job_ids?: string[];
    summary?: {
      completed?: number;
      failed?: number;
      cancelled?: number;
    };
  } | null;
}

/** Input chunk shape — the intersection of HistoryMessage + StreamChunk. */
export interface RawChunk {
  id?: string | null;
  role: string;
  content: string;
  attachments?: Attachment[] | null;
  tool_calls?: unknown[] | null;
  tool_call_id?: string;
  artifact?: Record<string, unknown> | null;
  invocation?: ToolInvocationEnvelope;
  activity?: MergedMessage['activity'];
  ts?: number;
  meta?: Record<string, unknown> | null;
}

export function sanitizeVisibleContent(content: string): string {
  return content || '';
}

function toolResultStatus(chunk: RawChunk): MergedToolCall['status'] {
  const persistedStatus = typeof chunk.meta?.status === 'string'
    ? chunk.meta.status
    : '';
  const invocationStatus = chunk.invocation?.status ?? '';
  const artifact = chunk.artifact;
  const status = artifact && typeof artifact.status === 'string'
    ? artifact.status
    : '';
  if (
    ['error', 'failed', 'errored', 'cancelled', 'canceled'].includes(persistedStatus)
    || ['error', 'cancelled'].includes(invocationStatus)
    || ['error', 'failed', 'errored', 'cancelled', 'canceled'].includes(status)
  ) {
    return 'error';
  }
  return 'done';
}

/**
 * Fold tool-result chunks into the assistant message that announced them.
 *
 * Walk-order matters: a `role: 'tool'` chunk attaches to the closest
 * preceding assistant message that announced a tool call with the matching
 * `tool_call_id`. We keep an `id → {messageIdx, callIdx}` index for O(1)
 * pairing without re-scanning the merged array on every tool chunk.
 *
 * Orphan tool chunks (no matching id) are silently dropped — they would
 * otherwise render as an unlabelled `'tool'` row that the user can't act
 * on, which is worse than nothing. A misordered or pre-T11 emitter that
 * loses an id is rare and not worth a special-case UI.
 */
export function mergeChunks(chunks: ReadonlyArray<RawChunk>): MergedMessage[] {
  const merged: MergedMessage[] = [];
  const index = new Map<string, { messageIdx: number; callIdx: number }>();

  for (const chunk of chunks) {
    if (chunk.role === 'tool') {
      if (chunk.tool_call_id) {
        const ref = index.get(chunk.tool_call_id);
        if (ref) {
          const owner = merged[ref.messageIdx];
          const call = owner.tool_calls[ref.callIdx];
          call.result = chunk.content;
          call.artifact = chunk.artifact ?? null;
          call.invocation = chunk.invocation ?? call.invocation;
          call.status = toolResultStatus(chunk);
          continue;
        }
      }
      // Orphan tool chunks drop silently. A missing/mismatched tool_call_id is a
      // backend protocol bug and should be fixed at the emitter instead of being
      // guessed here.
      continue;
    }

    // Anything not a `'tool'` chunk becomes a visible user/assistant message.
    const role: MergedMessage['role'] =
      chunk.role === 'user'
        ? 'user'
        : chunk.role === 'system'
          ? 'system'
          : 'assistant';
    const toolCalls: MergedToolCall[] = [];
    if (Array.isArray(chunk.tool_calls)) {
      for (const raw of chunk.tool_calls) {
        const tc = readRawToolCall(raw);
        if (!tc) continue;
        const id = tc.id ?? `__anon_${merged.length}_${toolCalls.length}`;
        const name = tc.function?.name ?? tc.name ?? '(unknown tool)';
        const args = argsToString(tc.function?.arguments ?? tc.arguments);
        toolCalls.push({
          id,
          name,
          arguments: args,
          status: 'running',
          invocation: tc.invocation,
        });
      }
    }
    // Assistant narration and tool activity are separate render items. Keep
    // user-visible narration even when the same protocol message also carries
    // tool calls; ChatMessageList groups only the tool calls, not the text.
    const visibleContent = role === 'assistant'
      ? sanitizeVisibleContent(chunk.content)
      : chunk.content;

    const messageIdx = merged.length;
    const previous = merged[merged.length - 1];
    if (
      role === 'assistant' &&
      toolCalls.length === 0 &&
      previous?.role === 'assistant' &&
      previous.tool_calls.length === 0
    ) {
      if (visibleContent === previous.content) {
        continue;
      }
      if (visibleContent.startsWith(previous.content)) {
        previous.content = visibleContent;
        previous.ts = chunk.ts ?? previous.ts;
        continue;
      }
      if (previous.content.startsWith(visibleContent)) {
        continue;
      }
    }

    const repeatedToolAnnounce =
      role === 'assistant' &&
      toolCalls.length > 0 &&
      previous?.role === 'assistant' &&
      previous.tool_calls.length > 0 &&
      toolCalls.every((call) =>
        previous.tool_calls.some((existing) => existing.id === call.id),
      );
    if (repeatedToolAnnounce) {
      for (const call of toolCalls) {
        const existingIdx = previous.tool_calls.findIndex(
          (existing) => existing.id === call.id,
        );
        if (existingIdx >= 0) {
          previous.tool_calls[existingIdx] = {
            ...previous.tool_calls[existingIdx],
            name: call.name,
            arguments: call.arguments,
            invocation: call.invocation ?? previous.tool_calls[existingIdx].invocation,
          };
          index.set(call.id, {
            messageIdx: merged.length - 1,
            callIdx: existingIdx,
          });
        }
      }
      continue;
    }

    if (!visibleContent && toolCalls.length === 0 && !chunk.attachments?.length) {
      continue;
    }

    merged.push({
      id: chunk.id,
      role,
      content: visibleContent,
      attachments: Array.isArray(chunk.attachments) ? chunk.attachments : [],
      tool_calls: toolCalls,
      ts: chunk.ts,
      activity: chunk.activity ?? null,
    });

    toolCalls.forEach((call, callIdx) => {
      index.set(call.id, { messageIdx, callIdx });
    });
  }

  return merged;
}
