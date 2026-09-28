import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useCreateChatSession, type ChatSessionsPage } from '@/lib/api/queries/chats';

afterEach(() => vi.unstubAllGlobals());

function setup() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(() => useCreateChatSession(), { wrapper }) };
}

describe('persisting new Project Chats', () => {
  it('publishes only a confirmed Chat and keeps retries idempotent in the list', async () => {
    const chat = { chat_id: 'chat-a', project_id: 'project-a', surface: 'chat', chat_context: 'New chat' };
    const fetch = vi.fn(async () => Response.json(chat));
    vi.stubGlobal('fetch', fetch);
    const { result, client } = setup();
    const input = { scopeId: 'carrier', chatId: 'chat-a', projectId: 'project-a' };
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await act(async () => { await result.current.mutateAsync(input); });
    }
    const page = client.getQueryData<ChatSessionsPage>(['chats', 'carrier', 'chat']);
    expect(page?.items).toEqual([chat]);
    expect(page?.total).toBe(1);
    expect(fetch.mock.calls[0]).toEqual([
      expect.stringContaining('/api/v1/chat-scopes/carrier/chats/chat-a'),
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ project_id: 'project-a' }) }),
    ]);
    client.clear();
  });

  it('lets the backend assign a browser Project and caches its returned identity', async () => {
    const chat = { chat_id: 'browser-a', project_id: 'auto-project', surface: 'browser' };
    const fetch = vi.fn(async () => Response.json(chat));
    vi.stubGlobal('fetch', fetch);
    const { result, client } = setup();
    await act(async () => { await result.current.mutateAsync({ scopeId: 'browser-carrier', chatId: 'browser-a' }); });
    expect(client.getQueryData<ChatSessionsPage>(['chats', 'browser-carrier', 'browser'])?.items).toEqual([chat]);
    expect(fetch.mock.calls[0]).toEqual([expect.any(String), expect.objectContaining({ body: '{"project_id":null}' })]);
    client.clear();
  });

  it('does not fabricate a local Chat after a failed creation request', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 403 })));
    const { result, client } = setup();
    await act(async () => {
      await expect(result.current.mutateAsync({ scopeId: 'carrier', chatId: 'denied', projectId: 'private' })).rejects.toThrow('403');
    });
    expect(client.getQueryData(['chats', 'carrier', 'chat'])).toBeUndefined();
    client.clear();
  });
});
