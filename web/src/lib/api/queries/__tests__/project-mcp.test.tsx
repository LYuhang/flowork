import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useProjectMcpSelection, useSetProjectMcpSelection } from '@/lib/api/queries/chats';

afterEach(() => vi.unstubAllGlobals());

function setup(projectId = 'project-a') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return {
    client,
    ...renderHook(({ id }) => ({ read: useProjectMcpSelection(id), write: useSetProjectMcpSelection() }), {
      wrapper, initialProps: { id: projectId },
    }),
  };
}

describe('Project MCP selection', () => {
  it('saves independently of sending a Chat message and reuses the Project query', async () => {
    let selection = { mcp_server_ids: [] as string[], mcp_config_revision: 0 };
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') selection = { ...JSON.parse(String(init.body)), mcp_config_revision: 1 };
      return Response.json(selection);
    });
    vi.stubGlobal('fetch', fetch);
    const { result, client, unmount } = setup();
    await waitFor(() => expect(result.current.read.isSuccess).toBe(true));
    await act(async () => {
      await result.current.write.mutateAsync({
        projectId: 'project-a', selection: { mcp_server_ids: ['server-a'], mcp_config_revision: 0 },
      });
    });
    expect(result.current.read.data).toEqual({ mcp_server_ids: ['server-a'], mcp_config_revision: 1 });
    expect(fetch.mock.calls.every(([url]) => url.endsWith('/api/v1/projects/project-a/mcp'))).toBe(true);
    unmount(); client.clear();
  });

  it('does not apply a delayed save to a different Project after navigation', async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        await pending;
        return Response.json({ mcp_server_ids: ['server-a'], mcp_config_revision: 1 });
      }
      return Response.json({ mcp_server_ids: [url.includes('project-b') ? 'server-b' : 'server-a'], mcp_config_revision: 0 });
    }));
    const { result, client, rerender, unmount } = setup();
    await waitFor(() => expect(result.current.read.isSuccess).toBe(true));
    let save!: Promise<unknown>;
    act(() => {
      save = result.current.write.mutateAsync({
        projectId: 'project-a', selection: { mcp_server_ids: ['server-a'], mcp_config_revision: 0 },
      });
    });
    rerender({ id: 'project-b' });
    await waitFor(() => expect(result.current.read.data?.mcp_server_ids).toEqual(['server-b']));
    await act(async () => { release(); await save; });
    expect(result.current.read.data?.mcp_server_ids).toEqual(['server-b']);
    expect(client.getQueryData(['project-mcp', 'project-a'])).toEqual({ mcp_server_ids: ['server-a'], mcp_config_revision: 1 });
    unmount(); client.clear();
  });

  it('reloads authoritative settings after a stale update is rejected', async () => {
    let reads = 0;
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') return Response.json({ detail: { error_code: 'mcp_config_revision_conflict' } }, { status: 409 });
      reads += 1;
      return Response.json({ mcp_server_ids: [reads > 1 ? 'newer-selection' : 'old-selection'], mcp_config_revision: reads });
    }));
    const { result, client, unmount } = setup();
    await waitFor(() => expect(result.current.read.isSuccess).toBe(true));
    await act(async () => {
      await expect(result.current.write.mutateAsync({
        projectId: 'project-a', selection: { mcp_server_ids: [], mcp_config_revision: 1 },
      })).rejects.toThrow('409');
    });
    await waitFor(() => expect(result.current.read.data?.mcp_server_ids).toEqual(['newer-selection']));
    unmount(); client.clear();
  });
});
