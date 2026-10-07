/** Task logs replay from the last accepted event after network interruptions. */
import { useEffect, useState } from "react";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { isSseDoneSentinel } from "./json";

import { useAuthStore } from "@/stores/auth";
import { getApiBase } from "@/lib/base-path";
import type { TaskEventPayload, TaskEventType } from "@/lib/api/tasks";

/** One row from `task_events`, plus the parsed payload JSON. */
export interface TaskEventFrame {
  /** Monotonic `task_events.id` — strictly increasing per insertion. */
  id: number;
  /** Event name (`state` | `progress` | `log` | `result` | `terminal`). */
  event_type: TaskEventType;
  /** Parsed `data:` payload. `null` if the frame had no body. */
  payload: TaskEventPayload;
}

/**
 * Terminal event types — once we see one, the task is done and the
 * stream is finished. We abort the controller so the library stops its
 * auto-retry loop.
 */
const TERMINAL_EVENT_TYPES = new Set<string>([
  "terminal",
]);

const TASK_EVENT_TYPES = new Set<string>([
  "state",
  "progress",
  "log",
  "result",
  "terminal",
]);

export interface UseTaskStreamResult {
  /** Events in arrival order — same as `task_events.id` order on the wire. */
  events: TaskEventFrame[];
  /** `true` once a terminal frame has arrived. */
  done: boolean;
}

export function useTaskStream(
  taskId: string | undefined,
  enabled = true,
  initialAfter = 0,
): UseTaskStreamResult {
  const [streamState, setStreamState] = useState<{
    taskId: string;
    events: TaskEventFrame[];
    done: boolean;
  } | null>(null);

  useEffect(() => {
    if (!taskId || !enabled) return;

    let cursor = initialAfter;

    const ctrl = new AbortController();
    const base = getApiBase();

    void (async function connect() {
      try {
        await fetchEventSource(`${base}/api/v1/tasks/${taskId}/stream`, {
          signal: ctrl.signal,
          credentials: "include",
          // The library records raw IDs before parsing. Always reconnect from
          // the last event accepted into our projection instead.
          fetch: (input, init) => {
            const headers = new Headers(init?.headers);
            headers.delete('Last-Event-ID');
            if (cursor > 0) headers.set('Last-Event-ID', String(cursor));
            return fetch(input, { ...init, headers });
          },
          headers: (() => {
            const h: Record<string, string> = {
              Accept: "text/event-stream",
            };
            const token = useAuthStore.getState().token;
            if (token) h.Authorization = `Bearer ${token}`;
            if (cursor > 0) {
              h["Last-Event-ID"] = String(cursor);
            }
            return h;
          })(),
          // Keep streaming when the tab is backgrounded — a long batch
          // run can take minutes and the user often tabs away.
          openWhenHidden: true,
          onopen: async (res) => {
            if (res.status === 204) {
              setStreamState((current) => ({
                taskId,
                events: current?.taskId === taskId ? current.events : [],
                done: true,
              }));
              ctrl.abort();
              return;
            }
            // 401 short-circuits before the body is read; matches the
            // agent-stream + exec-stream pattern.
            if (res.status === 401) {
              ctrl.abort();
              useAuthStore.getState().handle401();
              throw new Error("auth");
            }
            if (res.status === 403 || res.status === 404) ctrl.abort();
            if (!res.ok) {
              throw new Error(`task stream open failed: ${res.status}`);
            }
            if (!res.headers.get('content-type')?.startsWith('text/event-stream')) {
              throw new Error('Invalid task stream content type');
            }
          },
          onmessage(msg) {
            if (isSseDoneSentinel(msg.data)) return;
            // The backend always emits an `id:` line; skip frames that
            // somehow lose it (defence in depth — the SSE generator in
            // `sse_bridge.py` always sets it from `task_events.id`).
            if (!msg.id) return;
            const id = Number(msg.id);
            if (!Number.isSafeInteger(id) || id <= 0) throw new Error('Invalid task event ID');
            if (id <= cursor) return;
            const payload: unknown = JSON.parse(msg.data);
            if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
              throw new Error('Invalid task event payload');
            }

            const eventType = msg.event || "message";
            if (!TASK_EVENT_TYPES.has(eventType)) throw new Error("Invalid task event type");
            const frame: TaskEventFrame = {
              id,
              event_type: eventType as TaskEventType,
              payload: (payload && typeof payload === "object" ? payload : {}) as TaskEventPayload,
            };
            setStreamState((current) => ({
              taskId,
              events: current?.taskId === taskId
                ? current.events.some((event) => event.id === id) ? current.events : [...current.events, frame]
                : [frame],
              done: current?.taskId === taskId ? current.done : false,
            }));

            cursor = id;

            if (TERMINAL_EVENT_TYPES.has(eventType)) {
              setStreamState((current) => ({
                taskId,
                events: current?.taskId === taskId ? current.events : [frame],
                done: true,
              }));
              // Abort so fetch-event-source stops its retry loop —
              // we've seen a terminal event and the backend will
              // close the stream anyway.
              ctrl.abort();
            }
          },
          onclose() {
            if (!ctrl.signal.aborted) throw new Error('Task stream ended before terminal event');
          },
          onerror(err) {
            // If the controller is aborted (unmount or terminal frame),
            // re-throw so fetchEventSource exits. Otherwise return
            // undefined so the library applies its default backoff and
            // retries — the backend's resume-from-Last-Event-ID
            // contract makes that safe.
            if (ctrl.signal.aborted) throw err;
            // fall through → library default retry
          },
        });
      } catch {
        // Swallow — either an explicit abort (unmount / done) or an
        // unrecoverable error. The component already shows the task
        // status from the polled `getTask`, so a noisy toast here
        // would be redundant. Future: surface a small "stream lost"
        // banner if telemetry shows users hitting this often.
      }
    })();

    return () => {
      ctrl.abort();
    };
  }, [enabled, initialAfter, taskId]);

  if (!streamState || streamState.taskId !== taskId) return { events: [], done: false };
  return { events: streamState.events, done: streamState.done };
}
