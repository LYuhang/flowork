import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchAllChatSessions, useChatSessions } from '@/lib/api/queries/chats';
import { createElement, type PropsWithChildren } from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider, type QueryObserverOptions } from '@tanstack/react-query';

vi.mock('@/stores/auth', () => ({
  useAuthStore: { getState: () => ({ token: 'test-token', handle401: () => {} }) },
}));

afterEach(() => vi.restoreAllMocks());

describe('fetchAllChatSessions', () => {
  it('refreshes a lost browser lease until the server marks it inactive', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      items: [{ chat_id: 'chat-lost', browser_control_status: 'lost' }], total: 1,
    }), { status: 200 }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: PropsWithChildren) => createElement(QueryClientProvider, { client }, children);
    const { result, unmount } = renderHook(() => useChatSessions('scope', 'browser'), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const query = client.getQueryCache().find({ queryKey: ['chats', 'scope', 'browser'] })!;
    // useQuery installs observer options; Query.options exposes only their
    // narrower query-level base type, which omits refetchInterval.
    const interval = (query.options as QueryObserverOptions).refetchInterval;
    expect(typeof interval).toBe('function');
    if (typeof interval !== 'function') throw new Error('Missing reconnect refresh');
    expect(interval(query)).toBe(5000);
    client.setQueryData(['chats', 'scope', 'browser'], {
      items: [{ chat_id: 'chat-lost', browser_control_status: 'inactive' }], total: 1,
    });
    expect(interval(query)).toBe(false);
    unmount();
    client.clear();
  });
  it('continues through every server page instead of hiding older chats', async () => {
    const firstItems = Array.from({ length: 500 }, (_, index) => ({
      chat_id: `chat-${index}`,
    }));
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: firstItems,
        total: 501,
        limit: 500,
        offset: 0,
      }), { status: 200, headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{ chat_id: 'chat-500' }],
        total: 501,
        limit: 500,
        offset: 500,
      }), { status: 200, headers: { 'content-type': 'application/json' } }));

    const result = await fetchAllChatSessions('scope with spaces', 'chat');

    expect(result.items).toHaveLength(501);
    expect(result.items.at(-1)).toMatchObject({ chat_id: 'chat-500' });
    expect(result.total).toBe(501);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain(
      '/chat-scopes/scope%20with%20spaces/chats?surface=chat&limit=500&offset=0',
    );
    expect(String(fetchSpy.mock.calls[1]?.[0])).toContain('limit=500&offset=500');
  });
});
