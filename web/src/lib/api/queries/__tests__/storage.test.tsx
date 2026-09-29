import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { expect, it, vi } from 'vitest';
import { storageKeys, useWriteStorageContent } from '@/lib/api/queries/storage';

vi.mock('@/lib/api/storage', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api/storage')>(),
  writeStorageContent: vi.fn(async ({ path }: { path: string }) => ({
    path, size_bytes: 5, content_type: 'text/plain', replaced: true,
  })),
}));

it('keeps saved content available and fences an older in-flight read', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const key = storageKeys.content('/mount/notes.txt');
  const previous = { path: '/mount/notes.txt', content: 'before', content_type: 'text/plain', size_bytes: 6, truncated: false };
  client.setQueryData(key, previous);
  const listKey = storageKeys.list('/mount', '', 'name', null);
  client.setQueryData(listKey, { items: [] });
  let resolveRead!: (value: typeof previous) => void;
  const pending = client.fetchQuery({ queryKey: key, queryFn: () => new Promise<typeof previous>(resolve => { resolveRead = resolve; }) }).catch(() => undefined);
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result, unmount } = renderHook(() => useWriteStorageContent(), { wrapper });
  await act(async () => { await result.current.mutateAsync({ path: previous.path, content: 'after' }); });
  expect(client.getQueryData(key)).toEqual({ ...previous, content: 'after', size_bytes: 5 });
  expect(client.getQueryState(key)?.status).toBe('success');
  expect(client.getQueryState(listKey)?.isInvalidated).toBe(true);
  resolveRead(previous);
  await pending;
  expect(client.getQueryData(key)).toMatchObject({ content: 'after' });
  unmount();
  client.clear();
});
