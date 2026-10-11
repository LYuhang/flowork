import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatMessageSearch } from '../ChatMessageSearch';
import { searchChatMessages } from '@/lib/api/chat-engagement';
import i18n from '@/lib/i18n';

vi.mock('@/lib/api/chat-engagement', () => ({ searchChatMessages: vi.fn() }));
const search = vi.mocked(searchChatMessages);
const match = { id: 'old-message', role: 'user' as const, excerpt: '😀 needle text', match_start: 2, match_end: 8 };
beforeEach(async () => { search.mockReset(); await i18n.changeLanguage('en'); });
async function openSearch() {
  await userEvent.click(screen.getByRole('button', { name: 'Search conversation' }));
  await userEvent.type(screen.getByRole('textbox'), 'needle');
  await userEvent.click(screen.getByRole('button', { name: 'Search' }));
}

describe('conversation search', () => {
  it('waits for explicit submit rather than searching during input', async () => {
    search.mockResolvedValue({ items: [], scanned: 0, next_cursor: null });
    render(<ChatMessageSearch chatId="chat" open onOpenChange={vi.fn()} onLocate={vi.fn()} />);
    await userEvent.type(screen.getByRole('textbox'), 'needle');
    await new Promise(resolve => setTimeout(resolve, 350));
    expect(search).not.toHaveBeenCalled();
    await userEvent.keyboard('{Enter}');
    await waitFor(() => expect(search).toHaveBeenCalledOnce());
  });

  it('continues past an empty page and lists selectable highlighted candidates', async () => {
    search.mockResolvedValueOnce({ items: [], scanned: 100, next_cursor: 150 })
      .mockResolvedValueOnce({ items: [match, { ...match, id: 'next-message' }], scanned: 2, next_cursor: null });
    const locate = vi.fn();
    const { container } = render(<ChatMessageSearch chatId="chat" onLocate={locate} />);
    await openSearch();
    await screen.findByText('Matching messages: 2');
    expect(search).toHaveBeenNthCalledWith(2, 'chat', 'needle', 150, expect.any(AbortSignal));
    expect(container.querySelector('mark')).toHaveTextContent('needle');
    expect(container.querySelector('mark')).toHaveClass('text-state-danger');
    expect(screen.getAllByRole('button', { name: 'Locate matching message' })).toHaveLength(2);
    await userEvent.click(screen.getAllByRole('button', { name: 'Locate matching message' })[0]);
    expect(locate).toHaveBeenLastCalledWith('old-message');
    await userEvent.click(screen.getAllByRole('button', { name: 'Locate matching message' })[1]);
    expect(locate).toHaveBeenLastCalledWith('next-message');
    await userEvent.click(screen.getAllByRole('button', { name: 'Locate matching message' })[0]);
    expect(locate).toHaveBeenLastCalledWith('old-message');
  });

  it('aborts on close and ignores late results', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof searchChatMessages>>) => void;
    search.mockImplementation(() => new Promise(done => { resolve = done; }));
    render(<ChatMessageSearch chatId="chat" onLocate={vi.fn()} />);
    await openSearch();
    const signal = search.mock.calls[0][3];
    await userEvent.click(screen.getByRole('button', { name: 'Close search' }));
    expect(signal.aborted).toBe(true);
    resolve({ items: [match], scanned: 1, next_cursor: null });
    await userEvent.click(screen.getByRole('button', { name: 'Search conversation' }));
    expect(screen.queryByRole('button', { name: 'Locate matching message' })).toBeNull();
  });

  it('reports incomplete results on errors and does not automatically retry', async () => {
    search.mockResolvedValueOnce({ items: [match], scanned: 100, next_cursor: 100 }).mockRejectedValueOnce(new Error('offline'));
    render(<ChatMessageSearch chatId="chat" onLocate={vi.fn()} />);
    await openSearch();
    await screen.findByText(/Search failed/);
    expect(search).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Locate matching message' })).toBeVisible();
  });

  it('cancels the active request and prevents whitespace queries', async () => {
    search.mockImplementation(() => new Promise(() => {}));
    const { unmount } = render(<ChatMessageSearch chatId="chat" onLocate={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Search conversation' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: 'Search' })).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'needle' } });
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    await userEvent.click(screen.getByRole('button', { name: 'Stop search' }));
    await waitFor(() => expect(search.mock.calls[0][3].aborted).toBe(true));
    expect(screen.getByText(/Search stopped/)).toBeVisible();
    unmount();
  });
});
