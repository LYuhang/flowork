import { fetchEventSource } from '@microsoft/fetch-event-source';
import { useEffect, useRef } from 'react';

import { getApiBase } from '@/lib/base-path';
import { fileRefKey, type FileRefV1 } from '@/lib/preview/protocol';
import { useAuthStore } from '@/stores/auth';

export type PreviewFileEvent =
  | {
      type: 'ready';
      eventId: number;
      path: string;
      revision: string;
    }
  | {
      type: 'changed';
      eventId: number;
      path: string;
      changedPath: string;
      eventType: 'upsert' | 'delete';
      revision: string | null;
      derived: boolean;
      committedAt: string | null;
    };

type PreviewFileEventSubscriber = (event: PreviewFileEvent) => void;

type PreviewFileEventStream = {
  controller: AbortController;
  subscribers: Set<PreviewFileEventSubscriber>;
};

// Chat history can render the same artifact in several message cards while the
// Preview pane renders it again. Browsers allow only a small number of HTTP/1.1
// connections per origin; one SSE connection per component can therefore
// queue the Chat POST behind its own duplicate Preview streams. Share one
// transport per canonical FileRef and fan events out to every mounted view.
const previewFileEventStreams = new Map<string, PreviewFileEventStream>();

function eventUrl(fileRef: FileRefV1): string {
  const params = new URLSearchParams({
    scope: fileRef.scope,
    path: fileRef.path,
  });
  if (fileRef.scope === 'project') params.set('project_id', fileRef.projectId);
  if (fileRef.scope === 'run') params.set('run_id', fileRef.runId);
  return `${getApiBase()}/api/v1/previews/events?${params.toString()}`;
}

/**
 * Follow backend-originated VFS content changes for one FileRef.
 *
 * The server's first frame is a revision reconciliation, so reconnect, login
 * restoration, API worker replacement and the resolve/subscribe race require
 * no descriptor polling.
 */
export function usePreviewFileEvents(
  fileRef: FileRefV1,
  onEvent: (event: PreviewFileEvent) => void,
) {
  const authenticated = useAuthStore((state) => state.authenticated);
  const callbackRef = useRef(onEvent);
  useEffect(() => {
    callbackRef.current = onEvent;
  }, [onEvent]);

  const refKey = fileRefKey(fileRef);
  useEffect(() => {
    if (!authenticated) return;
    const subscriber: PreviewFileEventSubscriber = (event) => {
      callbackRef.current(event);
    };
    const existing = previewFileEventStreams.get(refKey);
    if (existing) {
      existing.subscribers.add(subscriber);
      return () => {
        existing.subscribers.delete(subscriber);
        if (existing.subscribers.size === 0) {
          existing.controller.abort();
          previewFileEventStreams.delete(refKey);
        }
      };
    }

    const stream: PreviewFileEventStream = {
      controller: new AbortController(),
      subscribers: new Set([subscriber]),
    };
    previewFileEventStreams.set(refKey, stream);
    let cursor = 0;
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
    };
    const publish = (event: PreviewFileEvent) => {
      for (const listener of stream.subscribers) listener(event);
    };

    void fetchEventSource(eventUrl(fileRef), {
      signal: stream.controller.signal,
      credentials: 'include',
      openWhenHidden: false,
      headers,
      onopen: async (response) => {
        if (response.status === 401) {
          useAuthStore.getState().handle401();
          throw new Error('auth');
        }
        if (!response.ok) {
          throw new Error(`preview event stream open failed: ${response.status}`);
        }
      },
      onmessage(message) {
        if (!message.data) return;
        try {
          const payload = JSON.parse(message.data) as Record<string, unknown>;
          const eventId = Number(payload.event_id);
          if (!Number.isFinite(eventId)) return;
          cursor = Math.max(cursor, eventId);
          headers['Last-Event-ID'] = String(cursor);
          if (
            message.event === 'preview_ready'
            && typeof payload.path === 'string'
            && typeof payload.revision === 'string'
          ) {
            publish({
              type: 'ready',
              eventId,
              path: payload.path,
              revision: payload.revision,
            });
          } else if (
            message.event === 'preview_file'
            && typeof payload.path === 'string'
            && typeof payload.changed_path === 'string'
            && (payload.event_type === 'upsert' || payload.event_type === 'delete')
          ) {
            const revision = typeof payload.revision === 'string'
              ? payload.revision
              : null;
            const committedAt = typeof payload.created_at === 'string'
              ? payload.created_at
              : null;
            publish({
              type: 'changed',
              eventId,
              path: payload.path,
              changedPath: payload.changed_path,
              eventType: payload.event_type,
              revision,
              derived: payload.derived === true,
              committedAt,
            });
          }
        } catch {
          // A later ready frame/reconnect remains authoritative.
        }
      },
      onclose() {
        if (!stream.controller.signal.aborted) {
          throw new Error('preview event stream closed');
        }
      },
      onerror(error) {
        if (stream.controller.signal.aborted) throw error;
        // Returning keeps fetch-event-source's bounded reconnect loop active.
      },
    }).catch(() => {
      // Auth failures are handled above. Transient transport failure is
      // reconciled by the first ready frame when this hook reconnects/remounts.
    });

    return () => {
      stream.subscribers.delete(subscriber);
      if (stream.subscribers.size === 0) {
        stream.controller.abort();
        if (previewFileEventStreams.get(refKey) === stream) {
          previewFileEventStreams.delete(refKey);
        }
      }
    };
    // fileRef is represented canonically by refKey; depending on the object
    // itself would reconnect for equivalent object literals on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authenticated, refKey]);
}
