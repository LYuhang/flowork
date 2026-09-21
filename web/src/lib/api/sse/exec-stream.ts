/**
 * Execution SSE stream — `POST .../executions` with an SSE response body.
 *
 * Why `@microsoft/fetch-event-source` and not the native `EventSource`:
 *   - `EventSource` is GET-only and cannot send a JSON body.
 *   - `EventSource` has no header support (so no `Authorization`).
 *   - `fetch-event-source` lets us set arbitrary request headers, pass an
 *     `AbortSignal`, and surface lifecycle hooks. Same rationale as
 *     `agent-stream.ts` (see that file's docstring for the OSS reference).
 *
 * Why not route exec events through `routeAgentSignal`:
 *   - Different endpoint, different lifecycle, different consumer store.
 *   - The agent router does chat-stream side-effects (buffer pushes,
 *     chat-history invalidation) that would be wrong here.
 *   - The four event names we care about (`started`, `EXEC_UPDATE`, `done`,
 *     `error`) are inlined in `onmessage` rather than going through a
 *     mini-router — the dispatch is trivial enough that a dedicated module
 *     would be ceremony, and matches the plan spec.
 *
 * This request is intentionally NOT retried. The endpoint is a POST that
 * creates a new execution as a side effect, so reconnecting would start the
 * workflow again.
 */
import { fetchEventSource } from '@microsoft/fetch-event-source';
import { unstable_batchedUpdates } from 'react-dom';
import { useAuthStore } from '@/stores/auth';
import { getApiBase } from '@/lib/base-path';
import { useExecStreamStore } from '@/stores/exec-stream';
import { queryClient } from '@/app/query-client';
import { isSseDoneSentinel, parseSseJson } from './json';

/** Local type guard mirroring the one in `route-signal.ts` / the store. */
function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null;
}

function isCancelledPayload(p: unknown): boolean {
  if (!isObject(p)) return false;
  return (
    p.code === 'cancelled' ||
    p.status === 'cancelled' ||
    p.status === 'stopped'
  );
}

function execStreamDebug(message: string, data?: unknown): void {
  const enabled =
    import.meta.env.VITE_EXEC_DEBUG === '1' ||
    (typeof window !== 'undefined' &&
      window.localStorage.getItem('vc_exec_debug') === '1');
  if (!enabled) return;

  console.info(`[workflow-exec] ${message}`, data ?? '');
}

export interface StreamExecutionArgs {
  wfId: string;
  input?: Record<string, unknown>;
  ac: AbortController;
}

/**
 * Start one execution and stream the per-node updates.
 *
 * Resolves after `event: done`. Rejects on transport errors or `ac.abort()`
 * (an AbortError). Callers in
 * `CanvasToolbar` discriminate AbortError from real failures and
 * downgrade aborts to `status: 'cancelled'` without a toast.
 */
export async function streamExecution(args: StreamExecutionArgs): Promise<void> {
  const token = useAuthStore.getState().token;
  const base = getApiBase();
  const url = `${base}/api/v1/workflows/${args.wfId}/executions`;

  // A whole-workflow run clears its fixed /run tier before staging new input.
  // Mirror that boundary in the client cache. Single-node execution does NOT
  // perform this prefix removal; it preserves the run and only refreshes the
  // one node file it overwrites.
  queryClient.removeQueries({
    queryKey: ['vfs', 'run-node-result', args.wfId],
  });

  // A workflow can finish many cheap nodes in the same few milliseconds.
  // Applying every SSE frame immediately makes ReactFlow + the Run panel
  // perform one render/layout per frame and can starve the browser main thread.
  // Coalesce frames into one animation-frame batch while preserving their
  // original order in the store.
  let queuedUpdates: unknown[] = [];
  let scheduledFrame: number | null = null;
  const flushUpdates = () => {
    if (scheduledFrame != null && typeof window !== 'undefined') {
      window.cancelAnimationFrame(scheduledFrame);
    }
    scheduledFrame = null;
    if (queuedUpdates.length === 0) return;
    const updates = queuedUpdates;
    queuedUpdates = [];
    unstable_batchedUpdates(() => {
      for (const update of updates) {
        useExecStreamStore.getState().applyUpdate(update);
      }
    });
  };
  const queueUpdate = (update: unknown) => {
    queuedUpdates.push(update);
    if (scheduledFrame != null) return;
    if (typeof window === 'undefined') {
      flushUpdates();
      return;
    }
    scheduledFrame = window.requestAnimationFrame(flushUpdates);
  };
  let cachesRefreshed = false;
  const refreshRunCaches = () => {
    if (cachesRefreshed) return;
    cachesRefreshed = true;
    void queryClient.invalidateQueries({
      queryKey: ['vfs', 'run-node-result', args.wfId],
    });
    void queryClient.invalidateQueries({
      queryKey: ['vfs', 'run-list', args.wfId],
    });
  };

  await fetchEventSource(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify({
      input: args.input ?? {},
    }),
    signal: args.ac.signal,
    // Keep streaming when the tab is backgrounded — execution can take
    // tens of seconds and the user often switches away while it runs.
    openWhenHidden: true,
    async onopen(res) {
      // 401 short-circuits before the body is read; see `agent-stream.ts`.
      if (res.status === 401) {
        useAuthStore.getState().handle401();
        throw new Error('auth');
      }
      if (!res.ok) {
        throw new Error(`execution stream failed with HTTP ${res.status}`);
      }
    },
    onmessage(ev) {
      if (isSseDoneSentinel(ev.data)) return;
      let payload: unknown;
      try {
        payload = parseSseJson(ev.data);
      } catch (err) {

        console.error('[workflow-exec] failed to parse SSE frame', {
          event: ev.event,
          id: ev.id,
          data: ev.data,
          err,
        });
        useExecStreamStore.getState().setStatus('error');
        throw err;
      }
      execStreamDebug('sse frame', { event: ev.event, id: ev.id, payload });
      if (ev.event === 'started') {
        useExecStreamStore.getState().begin(args.wfId, args.ac);
        // The server has now cleared the stable /run tier. Refresh the one
        // shared directory listing that gates every canvas node preview.
        queryClient.removeQueries({
          queryKey: ['vfs', 'run-list', args.wfId],
        });
        return;
      }
      if (ev.event === 'EXEC_UPDATE') {
        queueUpdate(payload);
        return;
      }
      if (ev.event === 'done') {
        flushUpdates();
        refreshRunCaches();
        // If a terminal EXEC_UPDATE already set completed/error/cancelled,
        // keep it. A bare `done` after per-node frames closes the workflow run
        // so the canvas leaves execution mode consistently with node execution.
        const st = useExecStreamStore.getState().status;
        if (st === 'running') useExecStreamStore.getState().setStatus('completed');
        return;
      }
      if (ev.event === 'error') {
        flushUpdates();
        refreshRunCaches();
        useExecStreamStore
          .getState()
          .setStatus(isCancelledPayload(payload) ? 'cancelled' : 'error');
        return;
      }
    },
    onerror(err) {
      // No retry: POST /executions is non-idempotent and a retry would create
      // another workflow run. Surface the transport failure to the caller.
      flushUpdates();
      refreshRunCaches();
      useExecStreamStore
        .getState()
        .setStatus(args.ac.signal.aborted ? 'cancelled' : 'error');
      throw err;
    },
  });
  flushUpdates();
  refreshRunCaches();
}
