import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';
import { TooltipProvider } from '@/components/ui/tooltip';
import { MessageActions } from '../MessageActions';
import { ChatShareDialog } from '../ChatShareDialog';
import { Markdown } from '../Markdown';
import type { ChatShare } from '@/lib/api/chat-engagement';
import { SharedChatPage } from '@/pages/chat/SharedChatPage';
import i18n from '@/lib/i18n';
import { getTimezone, setTimezone } from '@/lib/timezone';

beforeEach(async () => {
  await i18n.changeLanguage('en');
  server.use(http.post('*/api/v1/chats/engagement-chat/shares/preview', () => HttpResponse.json({
    messages: [{id: 'answer', role: 'assistant', content: 'Useful answer'}], existing: false,
  })));
});

function actions(timestamp?: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><TooltipProvider>
    <MessageActions chatId="engagement-chat" messageId="answer" content="Useful answer" timestamp={timestamp} />
  </TooltipProvider></QueryClientProvider>);
}

describe('message engagement', () => {
  it('shows the stored message time after Share in the selected timezone', () => {
    server.use(http.get('*/api/v1/chats/engagement-chat/feedback', () => HttpResponse.json({ ratings: {} })));
    const originalZone = getTimezone();
    act(() => setTimezone('Asia/Shanghai'));
    actions(Date.parse('2026-10-03T00:05:00Z') / 1000);
    const time = document.querySelector('[data-role="message-actions"] time');
    expect(time).toHaveAttribute('datetime', '2026-10-03T00:05:00.000Z');
    expect(time).toHaveTextContent('8:05');
    const share = screen.getByRole('button', {name: /Share this response/});
    expect(share.compareDocumentPosition(time!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    act(() => setTimezone(originalZone));
  });

  it.each([undefined, NaN])('omits missing or invalid timestamps (%s)', (timestamp) => {
    server.use(http.get('*/api/v1/chats/engagement-chat/feedback', () => HttpResponse.json({ ratings: {} })));
    actions(timestamp);
    expect(document.querySelector('[data-role="message-actions"] time')).toBeNull();
  });

  it.each(['en', 'zh'])('creates, copies and revokes a conversation share in %s', async (language) => {
    await i18n.changeLanguage(language);
    let items: ChatShare[] = [];
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    server.use(
      http.get('*/api/v1/chats/engagement-chat/shares', () => HttpResponse.json({ items })),
      http.post('*/api/v1/chats/engagement-chat/shares', async ({ request }) => {
        expect(await request.json()).toEqual({ message_id: null });
        items = [{ id: 'conversation-share', message_id: null, path: '/share/test',
          url: 'https://example.org/prefix/share/test', created_at: '2026-01-01T00:00:00Z',
          expires_at: null, message_count: 2 }];
        return HttpResponse.json(items[0]);
      }),
      http.delete('*/api/v1/chats/engagement-chat/shares/conversation-share', () => {
        items = [];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ChatShareDialog chatId="engagement-chat" /></QueryClientProvider>);
    fireEvent.click(screen.getByRole('button', { name: i18n.t('chat.share.title') }));
    expect(await screen.findByRole('dialog', { name: i18n.t('chat.share.title') })).toBeInTheDocument();
    expect(screen.getByText(i18n.t('chat.share.description'))).toBeInTheDocument();
    expect(screen.getByText(i18n.t('chat.share.exclusions'))).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('chat.share.create') }));
    expect(await screen.findByRole('textbox', { name: i18n.t('chat.share.link') })).toHaveValue('https://example.org/prefix/share/test');
    fireEvent.click(screen.getByRole('button', { name: i18n.t('chat.share.copy') }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://example.org/prefix/share/test'));
    fireEvent.click(screen.getByRole('button', { name: i18n.t('chat.share.revoke') }));
    expect(await screen.findByRole('button', { name: i18n.t('chat.share.create') })).toBeEnabled();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('restores the rating, toggles it off, and rolls back failed saves', async () => {
    let rating: string | null = 'up';
    server.use(
      http.get('*/api/v1/chats/engagement-chat/feedback', () => HttpResponse.json({ ratings: { answer: rating } })),
      http.put('*/api/v1/chats/engagement-chat/messages/answer/feedback', async ({ request }) => {
        const body = await request.json() as { rating: string | null };
        if (body.rating === 'down') return new HttpResponse(null, { status: 500 });
        rating = body.rating;
        return HttpResponse.json(body);
      }),
    );
    actions();
    const up = screen.getByRole('button', { name: 'Helpful' });
    const down = screen.getByRole('button', { name: 'Not helpful' });
    await waitFor(() => expect(up).toHaveAttribute('aria-pressed', 'true'));
    fireEvent.click(up);
    await waitFor(() => expect(rating).toBeNull());
    await waitFor(() => expect(up).toBeEnabled());
    expect(up).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(down);
    await waitFor(() => expect(down).toBeEnabled());
    expect(down).toHaveAttribute('aria-pressed', 'false');
  });

  it('previews a response before creating a link and allows explicit copying', async () => {
    let items: ChatShare[] = [];
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    server.use(
      http.get('*/api/v1/chats/engagement-chat/feedback', () => HttpResponse.json({ ratings: {} })),
      http.get('*/api/v1/chats/engagement-chat/shares', () => HttpResponse.json({items})),
      http.post('*/api/v1/chats/engagement-chat/shares', async ({ request }) => {
        expect(await request.json()).toEqual({ message_id: 'answer' });
        items = [{id: 's1', message_id: 'answer', path: '/share/token', url: 'https://example.org/prefix/share/token', created_at: '2026-01-01', message_count: 1, expires_at: null}];
        return HttpResponse.json({ id: 's1', path: '/share/token', url: 'https://example.org/prefix/share/token' });
      }),
    );
    actions();
    fireEvent.click(screen.getByRole('button', { name: 'Copy response' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Useful answer'));
    const share = screen.getByRole('button', { name: /Share this response/ });
    fireEvent.click(share);
    expect(await screen.findByRole('region', {name: 'Share preview'})).toHaveTextContent('Useful answer');
    expect(items).toEqual([]);
    expect(writeText).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', {name: 'Create response link'}));
    fireEvent.click(await screen.findByRole('button', {name: 'Copy link'}));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://example.org/prefix/share/token'));
    expect(screen.getByRole('button', {name: 'Revoke link'})).toBeInTheDocument();
  });

  it('does not load images or private file links in a public snapshot', () => {
    const { container } = render(<Markdown publicView>{'![tracking](https://tracker.example/pixel) [file](/api/private) [site](https://example.org)'}</Markdown>);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.queryByRole('link', { name: 'file' })).toBeNull();
    expect(screen.getByRole('link', { name: 'site' })).toHaveAttribute('rel', 'noreferrer');
  });

  it('renders an anonymous read-only transcript without a composer', async () => {
    server.use(http.get('*/api/v1/public/chat-shares/token', () => HttpResponse.json({
      title: 'Shared title', kind: 'conversation', messages: [{ id: 'a', role: 'assistant', content: 'Public answer' }],
    })));
    const client = new QueryClient();
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/share/token']}>
      <Routes><Route path="/share/:token" element={<SharedChatPage />} /></Routes>
    </MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText('Public answer')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: /send/i })).toBeNull();
    expect(document.querySelector('meta[name="robots"]')).toHaveAttribute('content', 'noindex, nofollow, noarchive');
  });
});
