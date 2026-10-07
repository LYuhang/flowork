// Resume a server-confirmed active turn on re-entry.
//
// The GET stream replays durable agent_run_events from PostgreSQL and then
// live-tails new events. Re-feeding those frames through routeAgentSignal
// rebuilds streaming text, tool cards, and pending HITL projection without
// relying on frontend-owned state as the source of truth.
import { fetchEventSource } from '@microsoft/fetch-event-source';
import { useAuthStore } from '@/stores/auth';
import { queryClient } from '@/app/query-client';
import { fetchChatHistory } from '@/lib/api/queries/chats';
import { useChatStreamStore } from '@/stores/chat-stream';
import { getApiBase } from '@/lib/base-path';
import { routeAgentSignal } from './route-signal';
import {
  type ActiveTurn,
  readActiveTurn,
  clearActiveTurn,
  updateActiveTurnCursor,
} from './active-turn';
import { isSseDoneSentinel, parseSseJson } from './json';
import { SseEventSequence, SseSequenceGapError } from './event-sequence';
import {
  getReservedTurnEventCursor,
  releaseTurnStream,
  reserveTurnEvent,
  tryAcquireTurnStream,
} from './turn-stream-coordinator';

class FatalResumeError extends Error {}

function isTransientResumeStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

// Reconciliation can be triggered by the periodic poll, focus, visibility,
// and the online event at nearly the same time. The coordinator below extends
// this replay-only promise dedupe across the POST submission stream as well:
// one Chat owns one frontend projection transport per page.
const inFlightResumes = new Map<string, Promise<boolean>>();

function applyPendingHitlProjection(turn: ActiveTurn): void {
  const pending = turn.pendingHitl ?? [];
  if (pending.length === 0) return;
  const store = useChatStreamStore.getState();
  for (const item of pending) {
    const event = item.uiProjectionEvent;
    if (
      item.status !== 'pending' ||
      event?.type !== 'tool_update' ||
      typeof event.tool_call_id !== 'string'
    ) {
      continue;
    }
    store.applyEvent({
      type: 'tool_update',
      tool_call_id: event.tool_call_id,
      content: typeof event.content === 'string' ? event.content : undefined,
      artifact: event.artifact && typeof event.artifact === 'object' && !Array.isArray(event.artifact)
        ? event.artifact as Record<string, unknown>
        : undefined,
      status: event.status === 'done' || event.status === 'error' || event.status === 'running'
        ? event.status
        : undefined,
    }, turn.chatId);
  }
}

/**
 * Try to resume the persisted active turn. Returns true if a running turn was
 * found and resumed (its `done`/`error` clears the persisted marker via
 * route-signal); false if it was already gone (404 → fall back to history).
 */
export async function resumeActiveTurn(
  at?: ActiveTurn,
  stream: typeof fetchEventSource = fetchEventSource,
): Promise<boolean> {
  const turn = at ?? readActiveTurn();
  if (!turn) return false;

  const resumeKey = `${turn.wfId}:${turn.chatId}:${turn.turnId}`;
  const existing = inFlightResumes.get(resumeKey);
  if (existing) return existing;

  const streamLease = tryAcquireTurnStream(turn.wfId, turn.chatId, 'resume');
  if (!streamLease) {
    // The POST submission stream (or another Run recovery for this Chat) is
    // already projecting the authoritative sequence. Reconciliation found a
    // real active Turn, so report it as resumed without opening a second tail.
    return true;
  }

  const resume = resumeActiveTurnStream(turn, stream).finally(() => {
    releaseTurnStream(streamLease);
    if (inFlightResumes.get(resumeKey) === resume) {
      inFlightResumes.delete(resumeKey);
    }
  });
  inFlightResumes.set(resumeKey, resume);
  return resume;
}

async function resumeActiveTurnStream(
  turn: ActiveTurn,
  stream: typeof fetchEventSource,
): Promise<boolean> {

  const token = useAuthStore.getState().token;
  const base = getApiBase();
  const url = `${base}/api/v1/chats/${turn.chatId}/turns/${turn.turnId}/stream`;

  const ac = new AbortController();
  let resumed = false;
  const existingRuntime = useChatStreamStore.getState().runtimes[turn.chatId];
  const samePageCursor = getReservedTurnEventCursor(
    turn.wfId,
    turn.chatId,
    turn.turnId,
  );
  const canContinueFromCursor =
    samePageCursor > 0
    || (existingRuntime?.state === 'streaming' && existingRuntime.messages.length > 0);
  let lastEventId = canContinueFromCursor
    ? Math.max(turn.lastEventId ?? 0, samePageCursor)
    : 0;
  // Show server-confirmed activity without changing the transcript boundary.
  if (turn.status) {
    const store = useChatStreamStore.getState();
    store.setState('streaming', turn.chatId);
    store.setWaitingForUser(turn.status === 'waiting_approval', turn.chatId);
    store.setAbort(ac, turn.chatId);
  }
  const eventSequence = new SseEventSequence(lastEventId);
  let terminalSeen = false;
  let consecutiveConnectFailures = 0;
  type PendingTextEvent = {
    event: string;
    payload: Record<string, unknown>;
    seq: number | null;
  };
  const pendingTextEvents: PendingTextEvent[] = [];
  let textFlushTimer: ReturnType<typeof globalThis.setTimeout> | null = null;

  const acknowledge = (seq: number | null) => {
    lastEventId = eventSequence.acknowledge(seq);
    if (seq !== null) updateActiveTurnCursor(turn, lastEventId);
  };

  const flushPendingTextEvents = () => {
    if (textFlushTimer !== null) {
      globalThis.clearTimeout(textFlushTimer);
      textFlushTimer = null;
    }
    while (pendingTextEvents.length > 0) {
      const pending = pendingTextEvents.shift();
      if (!pending) break;
      routeAgentSignal(pending.event, pending.payload, {
        wfId: turn.wfId,
        chatId: turn.chatId,
      });
      acknowledge(pending.seq);
    }
  };

  const scheduleTextFlush = () => {
    if (textFlushTimer !== null) return;
    // A reconnect may replay hundreds of durable token events immediately.
    // Coalesce adjacent text frames into a single macrotask so refresh recovery
    // does not block React with one store commit per historical token.
    textFlushTimer = globalThis.setTimeout(flushPendingTextEvents, 0);
  };

  const projectSignal = (event: string, payload: unknown, seq: number | null) => {
    if (
      event === 'CHAT_EVENT'
      && payload !== null
      && typeof payload === 'object'
      && typeof (payload as Record<string, unknown>).message_id === 'string'
    ) {
      const record = payload as Record<string, unknown>;
      const messageId = record.message_id as string;
      const previous = pendingTextEvents.at(-1);
      if (record.type === 'message_replace') {
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
        if (
          previous?.payload.type === 'message_delta'
          && previous.payload.message_id === messageId
        ) {
          previous.payload = {
            ...record,
            delta:
              (typeof previous.payload.delta === 'string' ? previous.payload.delta : '')
              + (typeof record.delta === 'string' ? record.delta : ''),
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
    routeAgentSignal(event, payload, { wfId: turn.wfId, chatId: turn.chatId });
    acknowledge(seq);
  };

  const waitBeforeReconnect = async () => {
    const exponent = Math.min(consecutiveConnectFailures, 4);
    consecutiveConnectFailures += 1;
    // Fast first recovery, bounded exponential backoff, and a little jitter so
    // many tabs/devices do not reconnect in lockstep after one proxy outage.
    const delay = Math.min(500 * (2 ** exponent), 5_000) + Math.random() * 250;
    await new Promise((resolve) => setTimeout(resolve, delay));
  };

  while (!terminalSeen && !ac.signal.aborted) {
    try {
      await stream(url, {
        method: 'GET',
        credentials: 'include',
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(lastEventId > 0 ? { 'Last-Event-ID': String(lastEventId) } : {}),
          Accept: 'text/event-stream',
        },
        signal: ac.signal,
        openWhenHidden: true,
        async onopen(res) {
          if (res.status === 404 || res.status === 410) {
            // The turn finished while we were away (buffer GC'd) — not running.
            throw new FatalResumeError('turn-gone');
          }
          if (res.status === 401) {
            useAuthStore.getState().handle401();
            throw new FatalResumeError('auth');
          }
          if (!res.ok) {
            // The resume endpoint is read-only and cursor based, so retrying a
            // transient gateway/rate-limit/dependency failure cannot create a
            // duplicate Turn. In particular, authorization dependencies may
            // briefly surface a 503 while the live Runtime keeps running.
            if (isTransientResumeStatus(res.status)) {
              throw new Error(`resume temporarily unavailable: ${res.status}`);
            }
            throw new FatalResumeError(`resume failed: ${res.status}`);
          }
          consecutiveConnectFailures = 0;
          // Install the fixed transcript boundary before exposing replayed
          // messages. fetch-event-source awaits onopen, so buffered events
          // cannot race ahead of this checkpoint.
          await queryClient.fetchQuery({
            queryKey: ['chat-history', turn.wfId, turn.chatId, turn.turnId],
            queryFn: () => fetchChatHistory(turn.wfId, turn.chatId, turn.turnId),
            staleTime: Infinity,
          });
          // Live turn found — wire abort (so STOP works) + set up the store for the
          // replay (chatId + streaming + a fresh buffer the replay re-fills).
          resumed = true;
          const store = useChatStreamStore.getState();
          store.setAbort(ac, turn.chatId);
          if (lastEventId > 0) {
            store.markStarted(turn.turnId, turn.chatId);
          } else {
            const runtime = store.runtimes[turn.chatId];
            if (runtime?.state === 'streaming' && runtime.messages.length > 0) {
              store.markStarted(turn.turnId, turn.chatId);
            } else {
              store.beginTurn(turn.chatId, turn.turnId);
              if (turn.inputMessage) {
                store.appendChunk({
                  role: 'user',
                  content: turn.inputMessage.content,
                  attachments: turn.inputMessage.attachments,
                }, turn.chatId);
              }
              store.markStarted(turn.turnId, turn.chatId);
            }
          }
          store.setWaitingForUser(
            turn.status === 'waiting_approval'
              || (turn.pendingHitl ?? []).some((item) => item.status === 'pending'),
            turn.chatId,
          );
          applyPendingHitlProjection(turn);
        },
        onmessage(ev) {
          if (isSseDoneSentinel(ev.data)) return;
          const received = eventSequence.receive(ev.id);
          if (received.duplicate) return;
          const payload: unknown = parseSseJson(ev.data);
          const isTerminal = ev.event === 'done' || ev.event === 'error';
          if (!reserveTurnEvent(
            turn.wfId,
            turn.chatId,
            turn.turnId,
            received.seq,
          )) {
            acknowledge(received.seq);
            if (isTerminal) terminalSeen = true;
            return;
          }
          projectSignal(ev.event, payload, received.seq);
          if (isTerminal) terminalSeen = true;
        },
        onerror(err) {
          throw err;
        },
      });
      flushPendingTextEvents();
      if (!terminalSeen && !ac.signal.aborted) {
        await waitBeforeReconnect();
      }
    } catch (err) {
      if (err instanceof FatalResumeError || ac.signal.aborted) break;
      // Commit every contiguous frame that was parsed before the transport
      // failed or exposed a later sequence gap. The retry starts after this
      // acknowledged prefix and therefore neither loses nor duplicates it.
      flushPendingTextEvents();
      if (err instanceof SseSequenceGapError) {
        console.warn('Resumed Agent SSE sequence gap; requesting replay', {
          chatId: turn.chatId,
          turnId: turn.turnId,
          expected: err.expected,
          received: err.received,
          lastAppliedEventId: eventSequence.cursor,
        });
      }
      eventSequence.rollbackUnacknowledged();
      lastEventId = eventSequence.cursor;
      if (!terminalSeen) {
        await waitBeforeReconnect();
      }
    }
  }
  flushPendingTextEvents();
  useChatStreamStore.getState().setAbort(null, turn.chatId);

  if (!resumed) {
    clearActiveTurn({ wfId: turn.wfId, chatId: turn.chatId });
    if (turn.status) useChatStreamStore.getState().setState('interrupted', turn.chatId);
  }
  return resumed;
}
