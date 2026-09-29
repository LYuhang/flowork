import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { queryClient } from '@/app/query-client';
import { useAuthStore } from '@/stores/auth';
import { writeRecentChatSelection, writeRecentDraftSelection } from '@/lib/chat/state-key';

const identity = { user_id: 'reader', tenant_id: 'workspace' };
const key = ['chat-history', 'scope', 'chat', null];

beforeEach(() => {
  queryClient.clear();
  sessionStorage.clear();
  window.history.replaceState({}, '', '/chat');
  useAuthStore.setState({ user: null, authenticated: false, bootstrapped: false });
});
afterEach(() => {
  queryClient.clear();
  sessionStorage.clear();
  window.history.replaceState({}, '', '/');
  vi.unstubAllGlobals();
});

it('shares the in-flight history request with the mounted chat query', async () => {
  writeRecentChatSelection(identity, 'chat', 'chat', 'scope');
  let resolveHistory!: (response: Response) => void;
  const history = new Promise<Response>((resolve) => { resolveHistory = resolve; });
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(identity)))
    .mockReturnValueOnce(history);
  vi.stubGlobal('fetch', fetchMock);
  await useAuthStore.getState().bootstrap();
  expect(queryClient.getQueryState(key)?.fetchStatus).toBe('fetching');
  const duplicate = vi.fn();
  const mounted = queryClient.fetchQuery({ queryKey: key, queryFn: duplicate });
  resolveHistory(new Response(JSON.stringify({ items: [], total: 0, limit: 30, offset: 0 })));
  await mounted;
  expect(duplicate).not.toHaveBeenCalled();
  expect(fetchMock).toHaveBeenCalledTimes(2); // auth + one history request
});

it('does not request history for an unsent draft', async () => {
  writeRecentDraftSelection(identity, { chatId: 'draft', scopeId: 'scope', projectId: 'project' });
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(identity)));
  vi.stubGlobal('fetch', fetchMock);
  await useAuthStore.getState().bootstrap();
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
