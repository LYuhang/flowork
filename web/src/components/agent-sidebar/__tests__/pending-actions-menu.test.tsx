import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { PendingActionsMenu } from '../PendingActionsMenu';
import { watchResourceActivity } from '@/lib/api/sse/resource-activity';
import { fetchPendingChatActions } from '@/lib/api/chat-engagement';

vi.mock('@/lib/api/chat-engagement', () => ({ fetchPendingChatActions: vi.fn() }));
vi.mock('@/lib/api/sse/resource-activity', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api/sse/resource-activity')>(),
  watchResourceActivity: vi.fn(() => vi.fn()),
}));
vi.mock('@/lib/timezone', () => ({ useFormatDateTime: () => (value: string) => value }));
const fetchActions = vi.mocked(fetchPendingChatActions);
const item = {
  hitl_request_id: 'approval-1', chat_id: 'chat-1', run_id: 'run-1', artifact_id: null,
  hitl_type: 'pre_tool_approval' as const, title: 'Approve export', created_at: '2026-10-10T12:00:00Z',
};
let client: QueryClient;
beforeEach(() => { vi.clearAllMocks(); client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); });
afterEach(() => { cleanup(); client.clear(); });
function setup(active = true) {
  const onSelect = vi.fn();
  const view = render(<QueryClientProvider client={client}><PendingActionsMenu scopeId="scope" active={active} onSelect={onSelect} /></QueryClientProvider>);
  return { ...view, onSelect, user: userEvent.setup() };
}
it('loads only when opened and selects the precise request without resolving it', async () => {
  fetchActions.mockResolvedValue({ items: [item], next_cursor: null });
  const { user, onSelect } = setup();
  expect(fetchActions).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Needs your attention' }));
  await user.click(await screen.findByText('Approve export'));
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(item);
  expect(fetchActions).toHaveBeenCalledExactlyOnceWith('scope', undefined);
});
it('keeps pagination available when an expired page contains no pending items', async () => {
  fetchActions.mockResolvedValueOnce({ items: [], next_cursor: 'expired-request' })
    .mockResolvedValueOnce({ items: [item], next_cursor: null });
  const { user } = setup();
  await user.click(screen.getByRole('button', { name: 'Needs your attention' }));
  await user.click(await screen.findByText('Load more requests'));
  expect(await screen.findByText('Approve export')).toBeVisible();
  expect(fetchActions).toHaveBeenLastCalledWith('scope', 'expired-request');
  expect(screen.queryByText('Nothing needs your attention.')).not.toBeInTheDocument();
});
it('exposes a failed read and retries only on an explicit click', async () => {
  fetchActions.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ items: [], next_cursor: null });
  const { user } = setup();
  await user.click(screen.getByRole('button', { name: 'Needs your attention' }));
  const retry = await screen.findByText('Could not load requests. Retry');
  expect(fetchActions).toHaveBeenCalledTimes(1);
  await user.click(retry);
  expect(await screen.findByText('Nothing needs your attention.')).toBeVisible();
  expect(fetchActions).toHaveBeenCalledTimes(2);
});
it('refreshes resolved requests on reopening instead of keeping an obsolete action', async () => {
  fetchActions.mockResolvedValueOnce({ items: [item], next_cursor: null })
    .mockResolvedValue({ items: [], next_cursor: null });
  const { user } = setup();
  await user.click(screen.getByRole('button', { name: 'Needs your attention' }));
  await screen.findByText('Approve export');
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: 'Needs your attention' }));
  await waitFor(() => expect(screen.queryByText('Approve export')).not.toBeInTheDocument());
  expect(await screen.findByText('Nothing needs your attention.')).toBeVisible();
});
it('does not fetch for a hidden panel', () => {
  setup(false);
  expect(screen.getByRole('button', { name: 'Needs your attention' })).toBeDisabled();
  expect(fetchActions).not.toHaveBeenCalled();
});

it('refreshes after another window resolves a request and unsubscribes on close', async () => {
  fetchActions.mockResolvedValueOnce({ items: [item], next_cursor: null })
    .mockResolvedValue({ items: [], next_cursor: null });
  const { user } = setup();
  await user.click(screen.getByRole('button', { name: 'Needs your attention' }));
  await screen.findByText('Approve export');
  const [path, refresh] = vi.mocked(watchResourceActivity).mock.calls[0];
  expect(path).toBe('/api/v1/chat-scopes/scope/attention/activity');
  await refresh();
  expect(await screen.findByText('Nothing needs your attention.')).toBeVisible();
  await user.keyboard('{Escape}');
  expect(vi.mocked(watchResourceActivity).mock.results[0].value).toHaveBeenCalledOnce();
});
