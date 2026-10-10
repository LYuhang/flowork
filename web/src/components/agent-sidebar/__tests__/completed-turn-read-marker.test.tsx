import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CompletedTurnReadMarker } from '../CompletedTurnReadMarker';
import { markChatTurnRead } from '@/lib/api/chat-engagement';
import type { ChatSessionsPage } from '@/lib/api/queries/chats';

vi.mock('@/lib/api/chat-engagement', () => ({ markChatTurnRead: vi.fn() }));
let intersect: (visible: boolean) => void;
const disconnect = vi.fn();
let client: QueryClient;
const key = ['chats', 'scope', 'chat'];
function show() {
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  client.setQueryData(key, { items: [{ chat_id: 'chat', latest_run: { run_id: 'turn', status: 'completed', unread: true } }] });
  return render(<QueryClientProvider client={client}><CompletedTurnReadMarker scopeId="scope" chatId="chat" runId="turn" /></QueryClientProvider>);
}
beforeEach(() => {
  vi.mocked(markChatTurnRead).mockReset().mockResolvedValue({ read_run_id: 'turn', read_created_at: '2026-10-10T00:00:00Z' });
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: IntersectionObserverCallback) { intersect = visible => callback([{ isIntersecting: visible } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
    observe() {} disconnect = disconnect;
  });
});
afterEach(() => { cleanup(); client?.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('completed turn read acknowledgement', () => {
  it('waits until the tail enters the viewport and acknowledges only once', async () => {
    const view = show();
    expect(markChatTurnRead).not.toHaveBeenCalled();
    act(() => intersect(false));
    expect(markChatTurnRead).not.toHaveBeenCalled();
    act(() => intersect(true));
    await waitFor(() => expect(markChatTurnRead).toHaveBeenCalledWith('chat', 'turn'));
    await waitFor(() => expect(client.getQueryData<ChatSessionsPage>(key)?.items[0].latest_run?.unread).toBe(false));
    act(() => { intersect(false); intersect(true); });
    expect(markChatTurnRead).toHaveBeenCalledTimes(1);
    view.unmount(); expect(disconnect).toHaveBeenCalled();
  });
  it('does not mark a background page read, then acknowledges on return', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    show(); act(() => intersect(true));
    expect(markChatTurnRead).not.toHaveBeenCalled();
    visibility.mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    await waitFor(() => expect(markChatTurnRead).toHaveBeenCalledTimes(1));
  });
  it('does not clear a newer unread run when an older request finishes', async () => {
    let finish!: (value: { read_run_id: string; read_created_at: string }) => void;
    vi.mocked(markChatTurnRead).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    show(); act(() => intersect(true));
    await waitFor(() => expect(markChatTurnRead).toHaveBeenCalledTimes(1));
    client.setQueryData(key, { items: [{ chat_id: 'chat', latest_run: { run_id: 'newer', status: 'completed', unread: true } }] });
    await act(async () => finish({ read_run_id: 'turn', read_created_at: '2026-10-10T00:00:00Z' }));
    expect(client.getQueryData<ChatSessionsPage>(key)?.items[0].latest_run?.unread).toBe(true);
  });
  it('retains unread after failure and retries only when the user requests it', async () => {
    vi.mocked(markChatTurnRead).mockRejectedValueOnce(new Error('offline'));
    show(); act(() => intersect(true));
    const retry = await screen.findByRole('button', { name: 'Could not update read status. Retry' });
    expect(client.getQueryData<ChatSessionsPage>(key)?.items[0].latest_run?.unread).toBe(true);
    act(() => { intersect(false); intersect(true); });
    expect(markChatTurnRead).toHaveBeenCalledTimes(1);
    fireEvent.click(retry);
    await waitFor(() => expect(markChatTurnRead).toHaveBeenCalledTimes(2));
  });
});
