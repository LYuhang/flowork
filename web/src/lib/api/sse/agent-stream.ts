/**
 * Agent SSE stream — `POST .../messages` with an SSE response body.
 *
 * Why `@microsoft/fetch-event-source` and not the native `EventSource`:
 *   - `EventSource` is GET-only and cannot send a JSON body.
 *   - `EventSource` has no header support (so no `Authorization`).
 *   - `fetch-event-source` is a thin wrapper around `fetch()` that streams
 *     the response through the same SSE parser, lets us set arbitrary
 *     request headers, pass an `AbortSignal`, and surface lifecycle hooks.
 *     Reference: https://github.com/Azure/fetch-event-source (used by
 *     Microsoft Copilot / Azure SDK for the same reason).
 *
 * T17 adds bounded retry with exponential backoff and flips the
 * chat-stream store to `'interrupted'` once we exhaust retries — the
 * `SSEStatusBanner` reads that state and surfaces the disconnect to the
 * user with a Dismiss action. `@microsoft/fetch-event-source`'s `onerror`
 * contract: return a number = wait ms before reconnect; throw = stop.
 *
 * `Attachment` is shaped to match the OpenAPI `Attachment` schema so
 * future TanStack Query inputs and this fetch can share the same type.
 */
import { fetchEventSource } from '@microsoft/fetch-event-source';
import { useAuthStore } from '@/stores/auth';
import { getApiBase } from '@/lib/base-path';
import { getTimezone } from '@/lib/timezone';
import { useChatStreamStore } from '@/stores/chat-stream';
import { getAgentSettings, getApprovalMode, type AgentSettings, type ApprovalMode } from '@/stores/agent-settings';
import type { components } from '@/lib/api/schema';
import { routeAgentSignal } from './route-signal';
import { readActiveTurnFor, rememberActiveTurn, updateActiveTurnCursor } from './active-turn';
import { resumeActiveTurn } from './resume-turn';
import { isSseDoneSentinel, parseSseJson } from './json';
import { SseEventSequence, SseSequenceGapError } from './event-sequence';
import {
  releaseTurnStream,
  reserveTurnEvent,
  tryAcquireTurnStream,
  type TurnStreamLease,
} from './turn-stream-coordinator';

/**
 * Build the `agent_settings` request block from the current runtime settings.
 * MCP servers and skills are tenant-installed integrations; the backend exposes
 * their lightweight catalogs automatically and the agent loads them on demand.
 */
export function buildAgentSettings(settings: AgentSettings = getAgentSettings()): Record<string, unknown> {
  // The model id is opaque and was supplied by the backend runtime catalog.
  // Credentials and provider secrets never cross this boundary.
  const s = settings;
  const agentSettings: Record<string, unknown> = {};
  if (s.modelId) agentSettings.model_id = s.modelId;
  if (s.temperature != null) agentSettings.temperature = s.temperature;
  if (s.maxTokens != null) agentSettings.max_tokens = s.maxTokens;
  if (s.timeout != null) agentSettings.timeout = s.timeout;
  if (s.reasoningEffort != null) agentSettings.reasoning_effort = s.reasoningEffort;
  return agentSettings;
}

type Attachment = NonNullable<components['schemas']['MessagePostBody']['attachments']>[number];

export type HitlContinueControl = components['schemas']['HitlContinueControl'];

interface RecoverRunResponse {
  run_id: string;
  chat_id: string;
  status: 'running' | 'waiting_approval' | 'cancel_requested' | 'completed' | 'cancelled' | 'failed';
  last_event_id?: number;
  pending_hitl?: Array<{
    hitl_request_id: string;
    hitl_type: string;
    status: string;
    title?: string;
    prompt_text?: string;
    ui_payload_json?: Record<string, unknown>;
    ui_projection_event_json?: Record<string, unknown>;
  }>;
}

export interface StreamAgentTurnArgs {
  wfId: string;
  chatId: string;
  /** Required only while the main-app Chat is being created. */
  projectId?: string | null;
  content: string;
  control?: HitlContinueControl;
  attachments?: Attachment[];
  /** Browser turns use the handed-off extension transport; Chat is default. */
  mode?: 'chat' | 'browser';
  /** Where the chat lives. `/browser` is side-panel-only — the side-panel embed
   *  sends "sidepanel"; the main app omits it (backend defaults to "main", which
   *  refuses `/browser` with a NOTICE). */
  surface?: 'main' | 'sidepanel';
  /** Product entry surface used by backend prompt/tool assembly. */
  agentSurface?: 'chat' | 'browser';
  /** Reserved authorization policy input; the official client currently auto-approves. */
  approvalMode?: ApprovalMode;
  /** Chat-scoped Runtime configuration. Settings only seed an unstarted Chat. */
  agentSettings?: AgentSettings;
  /** Called once the backend has atomically accepted the Turn and returned
   * its durable X-Turn-Id. This is intentionally earlier than stream end. */
  onAccepted?: () => void;
  signal: AbortSignal;
}

async function findTurnByClientRequest(args: {
  base: string;
  token: string | null;
  wfId: string;
  chatId: string;
  clientRequestId: string;
}) {
  const response = await fetch(
    `${args.base}/api/v1/chats/${encodeURIComponent(args.chatId)}` +
      `/turns/by-client-request/${encodeURIComponent(args.clientRequestId)}`,
    {
      headers: {
        ...(args.token ? { Authorization: `Bearer ${args.token}` } : {}),
        Accept: 'application/json',
      },
    },
  );
  if (response.status === 401) {
    useAuthStore.getState().handle401();
    return null;
  }
  if (response.status === 404) return null;
  if (!response.ok) return null;
  const row = await response.json() as RecoverRunResponse;
  if (!row.run_id || !row.chat_id) return null;
  return {
    wfId: args.wfId,
    chatId: row.chat_id,
    turnId: row.run_id,
    status: row.status,
    lastEventId: Number.isSafeInteger(row.last_event_id) && Number(row.last_event_id) > 0
      ? Number(row.last_event_id)
      : 0,
    pendingHitl: Array.isArray(row.pending_hitl)
      ? row.pending_hitl.map((item) => ({
          hitlRequestId: item.hitl_request_id,
          hitlType: item.hitl_type,
          status: item.status,
          title: item.title,
          promptText: item.prompt_text,
          uiPayload: item.ui_payload_json,
          uiProjectionEvent: item.ui_projection_event_json,
        }))
      : [],
  };
}

/**
 * Send one user turn and stream the agent's response.
 *
 * The promise resolves on `event: done` (after fetchEventSource closes the
 * stream) and rejects on transport / parser errors or `signal.abort()`.
 * Per-frame side-effects (buffer pushes, cache invalidations) happen
 * synchronously inside `onmessage` via `routeAgentSignal`.
 */
export async function streamAgentTurn(args: StreamAgentTurnArgs): Promise<void> {
  const streamLease = tryAcquireTurnStream(args.wfId, args.chatId, 'submission');
  if (!streamLease) {
    throw new Error('A live stream already owns this chat');
  }
  try {
    await streamOwnedAgentTurn(args, streamLease);
  } finally {
    releaseTurnStream(streamLease);
  }
}

async function streamOwnedAgentTurn(
  args: StreamAgentTurnArgs,
  streamLease: TurnStreamLease,
): Promise<void> {
  const token = useAuthStore.getState().token;
  const base = getApiBase();
  const url = `${base}/api/v1/chat-scopes/${args.wfId}/chats/${args.chatId}/messages`;

  const agentSettings = buildAgentSettings(args.agentSettings);
  const hasAgentSettings = Object.keys(agentSettings).length > 0;

  let terminalSeen = false;
  let unexpectedClose = false;
  let acceptedTurnId: string | null = null;
  const clientRequestId = args.control?.type === 'hitl_continue'
    ? `hitl_continue:${args.control.hitl_request_id}`
    : crypto.randomUUID();
  type PendingTextEvent = {
    event: string;
    payload: Record<string, unknown>;
    seq: number | null;
  };
  const pendingTextEvents: PendingTextEvent[] = [];
  let textFlushTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
  const eventSequence = new SseEventSequence();

  const acknowledge = (seq: number | null) => {
    const cursor = eventSequence.acknowledge(seq);
    if (seq === null || cursor !== seq) return;
    const activeTurn = readActiveTurnFor(args.wfId, args.chatId);
    if (activeTurn) updateActiveTurnCursor(activeTurn, cursor);
  };

  const flushPendingTextEvents = () => {
    if (textFlushTimer !== null) {
      globalThis.clearTimeout(textFlushTimer);
      textFlushTimer = null;
    }
    if (pendingTextEvents.length === 0) return;
    while (pendingTextEvents.length > 0) {
      const pending = pendingTextEvents.shift();
      if (!pending) break;
      routeAgentSignal(pending.event, pending.payload, {
        wfId: args.wfId,
        chatId: args.chatId,
      });
      acknowledge(pending.seq);
    }
  };

  const scheduleTextFlush = () => {
    if (textFlushTimer !== null) return;
    // A 50 ms cadence is perceptually smooth (20 fps) while leaving React
    // enough time to commit the growing transcript before the next token
    // batch. requestAnimationFrame is suspended in hidden/background tabs, so
    // use a timer while openWhenHidden keeps the transport alive.
    textFlushTimer = globalThis.setTimeout(flushPendingTextEvents, 50);
  };

  const dispatchSignal = (event: string, payload: unknown, seq: number | null) => {
    // Text tokens can arrive much faster than React can parse and commit the
    // Markdown tree. Updating Zustand once per character causes React to keep
    // restarting the same render and makes the answer appear only when the
    // stream ends. Project at most once per animation frame instead:
    // - cumulative message_replace keeps the latest value;
    // - incremental message_delta concatenates every delta in wire order.
    // Semantic boundaries flush synchronously so tool/end/done can never
    // overtake text.
    if (
      event === 'CHAT_EVENT' &&
      payload !== null &&
      typeof payload === 'object' &&
      typeof (payload as Record<string, unknown>).message_id === 'string'
    ) {
      const record = payload as Record<string, unknown>;
      const messageId = record.message_id as string;
      if (record.type === 'message_replace') {
        const previous = pendingTextEvents.at(-1);
        if (
          previous?.payload.type === 'message_replace'
          && previous.payload.message_id === messageId
        ) {
          previous.payload = record;
          previous.seq = seq;
        } else {
          pendingTextEvents.push({ event, payload: record, seq });
        }
        scheduleTextFlush();
        return;
      }
      if (record.type === 'message_delta') {
        const previous = pendingTextEvents.at(-1);
        if (
          previous?.payload.type === 'message_delta'
          && previous.payload.message_id === messageId
        ) {
          previous.payload = {
            ...record,
            delta:
              (typeof previous.payload.delta === 'string' ? previous.payload.delta : '') +
              (typeof record.delta === 'string' ? record.delta : ''),
          };
          previous.seq = seq;
        } else {
          pendingTextEvents.push({ event, payload: record, seq });
        }
        scheduleTextFlush();
        return;
      }
    }
    flushPendingTextEvents();
    routeAgentSignal(event, payload, {
      wfId: args.wfId,
      chatId: args.chatId,
    });
    acknowledge(seq);
  };

  try {
    await fetchEventSource(url, {
      method: 'POST',
      credentials: 'include',
      headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify({
      role: 'user',
      content: args.content,
      ...(args.projectId ? { project_id: args.projectId } : {}),
      ...(args.control ? { control: args.control } : {}),
      client_request_id: clientRequestId,
      attachments: args.attachments ?? [],
      mode: args.mode ?? 'chat',
      approval_mode: args.approvalMode ?? getApprovalMode(),
      timezone: getTimezone(),
      ...(args.surface ? { surface: args.surface } : {}),
      ...(args.agentSurface ? { agent_surface: args.agentSurface } : {}),
      ...(hasAgentSettings ? { agent_settings: agentSettings } : {}),
    }),
    signal: args.signal,
    // Keep the connection alive when the tab is hidden (e.g. user
    // switches windows mid-stream); without this the lib pauses streaming.
    openWhenHidden: true,
    async onopen(res) {
      // 401 short-circuits before the body is read: the token is bad and
      // no amount of retrying will fix it. `handle401()` clears the token
      // and opens the auth dialog; throwing here prevents fetch-event-source
      // from reconnecting on its own.
      if (res.status === 401) {
        useAuthStore.getState().handle401();
        throw new Error('auth');
      }
      if (!res.ok) {
        throw new Error(`agent turn rejected: ${res.status}`);
      }
      const headerTurnId = res.headers.get('X-Turn-Id');
      if (headerTurnId) {
        acceptedTurnId = headerTurnId;
        rememberActiveTurn({
          wfId: args.wfId,
          chatId: args.chatId,
          turnId: headerTurnId,
        });
        useChatStreamStore.getState().markStarted(headerTurnId, args.chatId);
        args.onAccepted?.();
      }
    },
    onmessage(ev) {
      if (isSseDoneSentinel(ev.data)) return;
      const received = eventSequence.receive(ev.id);
      if (received.duplicate) {
        console.debug('Ignored replayed SSE frame', {
          chatId: args.chatId,
          turnId: readActiveTurnFor(args.wfId, args.chatId)?.turnId,
          eventId: received.seq,
        });
        return;
      }
      const payload: unknown = parseSseJson(ev.data);
      const turnId = acceptedTurnId
        ?? readActiveTurnFor(args.wfId, args.chatId)?.turnId
        ?? clientRequestId;
      const isTerminal = ev.event === 'done' || ev.event === 'error';
      if (!reserveTurnEvent(args.wfId, args.chatId, turnId, received.seq)) {
        acknowledge(received.seq);
        if (isTerminal) terminalSeen = true;
        return;
      }
      dispatchSignal(ev.event, payload, received.seq);
      if (isTerminal) terminalSeen = true;
    },
    onclose() {
      const runtime = useChatStreamStore.getState().runtimes[args.chatId];
      if (!terminalSeen && runtime?.state === 'streaming') {
        unexpectedClose = true;
      }
    },
    onerror(err) {
      // This endpoint is POST and creates a new agent turn. Retrying here would
      // resend the user's message and start a second backend turn. Recovery must
      // use the turn_id recorded from X-Turn-Id / started and the GET resume
      // endpoint instead.
      throw err;
    },
    });
  } catch (err) {
    if ((err as { name?: string }).name === 'AbortError') {
      flushPendingTextEvents();
      throw err;
    }
    if (err instanceof SseSequenceGapError) {
      console.warn('Agent SSE sequence gap; replaying from last applied event', {
        chatId: args.chatId,
        expected: err.expected,
        received: err.received,
        lastAppliedEventId: eventSequence.cursor,
      });
    }
    unexpectedClose = true;
  }

  flushPendingTextEvents();

  if (!terminalSeen && unexpectedClose) {
    // The backend Run row is authoritative after a POST transport closes. A
    // cleanly persisted terminal event can race with the final SSE bytes (or a
    // browser/proxy can drop only that final frame). Consulting the Run also
    // prevents an old localStorage "running" marker from opening an endless
    // resume loop after the server has already completed the Turn.
    const recoveredRun = await findTurnByClientRequest({
      base,
      token,
      wfId: args.wfId,
      chatId: args.chatId,
      clientRequestId,
    });
    const localTurn = readActiveTurnFor(args.wfId, args.chatId);
    const recoveredTurn = recoveredRun ?? localTurn;
    if (recoveredTurn) {
      const serverStatus = recoveredRun?.status;
      const serverCursor = recoveredRun?.lastEventId ?? 0;
      if (
        serverStatus === 'completed'
        && serverCursor <= eventSequence.cursor
      ) {
        routeAgentSignal('done', {}, { wfId: args.wfId, chatId: args.chatId });
        return;
      }
      if (
        (serverStatus === 'cancelled' || serverStatus === 'failed')
        && serverCursor <= eventSequence.cursor
      ) {
        routeAgentSignal('error', {
          code: serverStatus === 'cancelled' ? 'cancelled' : 'engine_error',
          message: serverStatus === 'cancelled'
            ? 'Turn cancelled by client.'
            : 'Agent turn failed.',
        }, { wfId: args.wfId, chatId: args.chatId });
        return;
      }
      // Keep the in-memory acknowledged cursor even when localStorage is
      // unavailable or its write was delayed. Replaying from zero on top of an
      // already-mounted projection would duplicate message_delta content.
      const turn = {
        ...recoveredTurn,
        status:
          recoveredTurn.status === 'running'
          || recoveredTurn.status === 'waiting_approval'
          || recoveredTurn.status === 'cancel_requested'
            ? recoveredTurn.status
            : undefined,
        // Resume after the last frame this page actually projected, not after
        // the server's terminal high-water mark. The latter describes what is
        // durable, not what the browser has seen.
        lastEventId: Math.max(localTurn?.lastEventId ?? 0, eventSequence.cursor),
      };
      rememberActiveTurn(turn);
      // Transfer ownership only after every parsed frame above has been
      // flushed and acknowledged. The resume stream can now safely continue
      // from that cursor without running beside the POST stream.
      releaseTurnStream(streamLease);
      const resumed = await resumeActiveTurn(turn);
      if (resumed) return;
    }
    useChatStreamStore.getState().setState('interrupted', args.chatId);
    return;
  }
}
