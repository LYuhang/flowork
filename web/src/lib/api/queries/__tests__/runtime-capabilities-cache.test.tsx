import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useAgentRuntimeCapabilities } from '../agent-runtime';

afterEach(() => vi.unstubAllGlobals());

function setup(persisted: boolean, projectId: string | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, ...renderHook(({ project }) => useAgentRuntimeCapabilities('chat-1', { persisted, projectId: project }), {
    wrapper, initialProps: { project: projectId },
  }) };
}

describe('runtime capability identity', () => {
  it('keeps a persisted Chat request when its Project metadata arrives later', async () => {
    const fetch = vi.fn(async (_url: string) => Response.json({ runtime_type: 'codex', models: [] }));
    vi.stubGlobal('fetch', fetch);
    const { client, result, rerender, unmount } = setup(true, null);
    try {
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      rerender({ project: 'project-1' });
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(String(fetch.mock.calls[0]?.[0])).toContain('chat_id=chat-1');
      expect(String(fetch.mock.calls[0]?.[0])).not.toContain('project_id');
    } finally { unmount(); client.clear(); }
  });

  it('keeps Project-specific capabilities for a draft with a preallocated Chat id', async () => {
    const fetch = vi.fn(async (url: string) => Response.json({ runtime_type: 'codex', models: [], marker: url }));
    vi.stubGlobal('fetch', fetch);
    const { client, result, rerender, unmount } = setup(false, 'project-1');
    try {
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      rerender({ project: 'project-2' });
      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
      expect(fetch.mock.calls[0][0]).toContain('project_id=project-1');
      expect(fetch.mock.calls[1][0]).toContain('project_id=project-2');
    } finally { unmount(); client.clear(); }
  });
});
