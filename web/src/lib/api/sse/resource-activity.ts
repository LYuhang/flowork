import { fetchEventSource } from '@microsoft/fetch-event-source';
import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import type { QueryClient } from '@tanstack/react-query';

/** Drain an older read, then read the state invalidated by this event. */
export async function refreshResourceQuery(client: QueryClient, queryKey: readonly unknown[]) {
  const filter = { queryKey, exact: true };
  const options = { cancelRefetch: false, throwOnError: true };
  if (client.isFetching(filter)) await client.refetchQueries(filter, options);
  await client.invalidateQueries(filter, options);
}

/** Snapshot invalidations: coalesce bursts, preserve changes during a read. */
export function watchResourceActivity(path: string, refresh: () => Promise<unknown>, coalesceMs = 100): () => void {
  const ctrl = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let dirty = false;
  let reading = false;
  let retryMs = 0;
  const schedule = () => {
    if (timer || reading || ctrl.signal.aborted) return;
    timer = setTimeout(() => {
      timer = undefined;
      if (ctrl.signal.aborted) return;
      dirty = false;
      reading = true;
      void refresh().then(() => { retryMs = 0; }).catch(() => {
        // The notification may have arrived even when its snapshot request
        // failed. Keep that invalidation until a read succeeds.
        dirty = true;
        retryMs = Math.min(retryMs ? retryMs * 2 : 1000, 30_000);
      }).finally(() => {
        reading = false;
        if (dirty) schedule();
      });
    }, Math.max(coalesceMs, retryMs));
  };
  const token = useAuthStore.getState().token;
  void fetchEventSource(`${getApiBase()}${path}`, {
    signal: ctrl.signal,
    credentials: 'include',
    openWhenHidden: true,
    headers: { Accept: 'text/event-stream', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    async onopen(response) {
      if ([401, 403, 404].includes(response.status)) ctrl.abort();
      if (response.status === 401) useAuthStore.getState().handle401();
      if (!response.ok || !response.headers.get('content-type')?.startsWith('text/event-stream')) {
        throw new Error('Resource activity connection failed');
      }
    },
    onmessage(message) {
      if (message.event !== 'changed') return;
      dirty = true;
      schedule();
    },
    onclose() { throw new Error('Resource activity disconnected'); },
    onerror() { return 2000; },
  }).catch(() => {});
  return () => {
    ctrl.abort();
    if (timer) clearTimeout(timer);
  };
}
