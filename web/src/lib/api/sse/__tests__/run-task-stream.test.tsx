import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';
import { useTaskStream } from '../run-task-stream';

const frame = (id: number, type = 'log') => `id: ${id}\nevent: ${type}\ndata: {"message":"row-${id}"}\n\n`;
const response = (body: string) => new HttpResponse(body, { headers: { 'Content-Type': 'text/event-stream' } });

describe('Task stream network recovery', () => {
  it('closes when the final event is already in the loaded history', async () => {
    let requests = 0;
    server.use(http.get('*/api/v1/tasks/complete/stream', ({ request }) => {
      requests++;
      expect(request.headers.get('Last-Event-ID')).toBe('42');
      return new HttpResponse(null, { status: 204 });
    }));
    const { result, unmount } = renderHook(() => useTaskStream('complete', true, 42));
    try {
      await waitFor(() => expect(result.current.done).toBe(true));
      expect(result.current.events).toEqual([]);
      expect(requests).toBe(1);
    } finally { unmount(); }
  });

  it('reconnects after premature EOF and deduplicates replayed rows', async () => {
    const cursors: (string | null)[] = [];
    server.use(http.get('*/api/v1/tasks/recover/stream', ({ request }) => {
      cursors.push(request.headers.get('Last-Event-ID'));
      return response(cursors.length === 1 ? frame(11) : frame(11) + frame(14) + frame(18, 'terminal'));
    }));
    const { result, unmount } = renderHook(() => useTaskStream('recover'));
    try {
      await waitFor(() => expect(result.current.done).toBe(true), { timeout: 4000 });
      expect(cursors).toEqual([null, '11']);
      expect(result.current.events.map((row) => row.id)).toEqual([11, 14, 18]);
    } finally { unmount(); }
  });

  it('does not acknowledge a frame whose JSON could not be parsed', async () => {
    const cursors: (string | null)[] = [];
    server.use(http.get('*/api/v1/tasks/partial/stream', ({ request }) => {
      cursors.push(request.headers.get('Last-Event-ID'));
      return response(cursors.length === 1
        ? frame(21) + 'id: 25\nevent: log\ndata: {broken\n\n'
        : frame(25) + frame(29, 'terminal'));
    }));
    const { result, unmount } = renderHook(() => useTaskStream('partial'));
    try {
      await waitFor(() => expect(result.current.done).toBe(true), { timeout: 4000 });
      expect(cursors).toEqual([null, '21']);
      expect(result.current.events.map((row) => row.id)).toEqual([21, 25, 29]);
    } finally { unmount(); }
  });
});
