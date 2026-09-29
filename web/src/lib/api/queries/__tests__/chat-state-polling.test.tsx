import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useChatState, type BackgroundJob } from '../chats';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

function state(status: BackgroundJob['status']) {
  return { todo_items: [], active_modes: [], background_jobs: [{ job_id: 'job-1', status }] };
}

function setup(status: BackgroundJob['status']) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(['chat-state', 'scope-1', 'chat-1'], state(status));
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, ...renderHook(() => useChatState('scope-1', 'chat-1'), { wrapper }) };
}

describe('chat background job polling', () => {
  it.each(['completed', 'failed', 'cancelled'] as const)('does not poll for retained %s jobs', async (status) => {
    vi.useFakeTimers();
    const fetch = vi.fn(async () => Response.json(state(status)));
    vi.stubGlobal('fetch', fetch);
    const { client, unmount } = setup(status);
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(fetch).not.toHaveBeenCalled();
    } finally { unmount(); client.clear(); }
  });

  it.each(['queued', 'running', 'cancelling'] as const)('polls %s jobs until the response becomes terminal', async (status) => {
    vi.useFakeTimers();
    const fetch = vi.fn(async () => Response.json(state('completed')));
    vi.stubGlobal('fetch', fetch);
    const { client, unmount, result } = setup(status);
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(result.current.data?.background_jobs[0].status).toBe('completed');
    } finally { unmount(); client.clear(); }
  });
});
