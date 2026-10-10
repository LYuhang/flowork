import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { toast } from 'sonner';
import { server } from '@/__tests__/msw-handlers';
import { useChatSessions } from '@/lib/api/queries/chats';
import { useChatStreamStore } from '@/stores/chat-stream';
import { ChatHistoryMenu } from '../ChatHistoryMenu';
import { ArchivedChatNotice } from '../ArchivedChatNotice';

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));
const stamp = '2026-10-10T12:00:00Z';
let rows: { chat_id: string; chat_context: string; archived_at: string | null; pinned_at: string | null }[];
let patches: { id: string; body: Record<string, boolean> }[];
let fail: boolean;
let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  rows = [
    { chat_id: 'newer', chat_context: 'Newer chat', archived_at: null, pinned_at: null },
    { chat_id: 'current', chat_context: 'Current conversation', archived_at: null, pinned_at: null },
    { chat_id: 'archived', chat_context: 'Older archive', archived_at: stamp, pinned_at: null },
  ];
  patches = []; fail = false; vi.clearAllMocks();
  useChatStreamStore.setState({ runtimes: {} });
  server.use(
    http.get('*/api/v1/chat-scopes/scope/chats', () => HttpResponse.json({
      items: [...rows].sort((a, b) => Number(Boolean(b.pinned_at)) - Number(Boolean(a.pinned_at))),
      total: rows.length, limit: 500, offset: 0,
    })),
    http.patch('*/api/v1/chats/:id/list-state', async ({ params, request }) => {
      const body = await request.json() as Record<string, boolean>;
      patches.push({ id: String(params.id), body });
      if (fail) return HttpResponse.json({ detail: 'denied' }, { status: 403 });
      const row = rows.find(item => item.chat_id === params.id)!;
      if (body.pinned !== undefined) row.pinned_at = body.pinned ? stamp : null;
      if (body.archived !== undefined) row.archived_at = body.archived ? stamp : null;
      return HttpResponse.json({ pinned_at: row.pinned_at, archived_at: row.archived_at });
    }),
  );
});
afterEach(() => { cleanup(); client.clear(); });
function setup() {
  const select = vi.fn();
  function Harness() {
    const sessions = useChatSessions('scope');
    return <><ChatHistoryMenu wfId="scope" activeChatId="current" onSelect={select} />
      <ArchivedChatNotice chat={sessions.data?.items.find(item => item.chat_id === 'current')} /></>;
  }
  render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>);
  return { user: userEvent.setup(), select };
}
async function openHistory(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Chat History' }));
  await screen.findByRole('menuitem', { name: 'Newer chat' });
}
it('filters archived chats without changing their stored state or selecting them', async () => {
  const { user, select } = setup(); await openHistory(user);
  expect(screen.queryByText('Older archive')).not.toBeInTheDocument();
  await user.click(screen.getByRole('menuitemradio', { name: 'Archived chats' }));
  expect(await screen.findByText('Older archive')).toBeVisible();
  expect(screen.queryByText('Newer chat')).not.toBeInTheDocument();
  await user.click(screen.getByText('Older archive'));
  expect(select).toHaveBeenCalledExactlyOnceWith('archived');
  expect(patches).toEqual([]);
});
// jsdom has no submenu geometry for Radix's pointer-grace region. Activate
// the item directly here; browser acceptance must cover actual pointer travel.
it('pins through the row action without opening the conversation', async () => {
  const { user, select } = setup(); await openHistory(user);
  await user.click(screen.getByRole('menuitem', { name: 'Actions for conversation Current conversation' }));
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Pin' }));
  await waitFor(() => expect(patches).toEqual([{ id: 'current', body: { pinned: true } }]));
  expect(select).not.toHaveBeenCalled();
  await openHistory(user);
  await waitFor(() => expect(screen.getAllByRole('menuitem').filter(item => item.hasAttribute('data-chat-id'))[0]).toHaveAttribute('data-chat-id', 'current'));
  await user.click(screen.getByRole('menuitem', { name: 'Actions for conversation Current conversation' }));
  expect(await screen.findByRole('menuitem', { name: 'Unpin' })).toBeVisible();
});
it('archives a running chat and restores it from the notice without changing the run', async () => {
  useChatStreamStore.getState().beginTurn('current', 'live-turn');
  const { user } = setup(); await openHistory(user);
  await user.click(screen.getByRole('menuitem', { name: 'Actions for conversation Current conversation' }));
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive conversation' }));
  expect(await screen.findByText('This conversation is archived.')).toBeVisible();
  expect(useChatStreamStore.getState().runtimes.current.state).toBe('streaming');
  await user.click(screen.getByRole('button', { name: 'Restore conversation' }));
  await waitFor(() => expect(screen.queryByText('This conversation is archived.')).toBeNull());
  expect(patches).toEqual([{ id: 'current', body: { archived: true } }, { id: 'current', body: { archived: false } }]);
  expect(useChatStreamStore.getState().runtimes.current.turnId).toBe('live-turn');
});
it('keeps the list unchanged and reports a failed mutation without retrying', async () => {
  fail = true; const { user } = setup(); await openHistory(user);
  await user.click(screen.getByRole('menuitem', { name: 'Actions for conversation Current conversation' }));
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive conversation' }));
  await waitFor(() => expect(toast.error).toHaveBeenCalledOnce());
  expect(patches).toHaveLength(1); expect(rows[1].archived_at).toBeNull();
  expect(screen.queryByText('This conversation is archived.')).toBeNull();
});
