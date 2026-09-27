import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';
import { TooltipProvider } from '@/components/ui/tooltip';
import { MessageActions } from '../MessageActions';
import { Markdown } from '../Markdown';
import { SharedChatPage } from '@/pages/chat/SharedChatPage';
import i18n from '@/lib/i18n';

beforeEach(async () => { await i18n.changeLanguage('en'); });

function actions() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><TooltipProvider>
    <MessageActions chatId="engagement-chat" messageId="answer" content="Useful answer" />
  </TooltipProvider></QueryClientProvider>);
}

describe('message engagement', () => {
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

  it('copies a response and a single-message share link, with transient success feedback', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    server.use(
      http.get('*/api/v1/chats/engagement-chat/feedback', () => HttpResponse.json({ ratings: {} })),
      http.post('*/api/v1/chats/engagement-chat/shares', async ({ request }) => {
        expect(await request.json()).toEqual({ message_id: 'answer' });
        return HttpResponse.json({ id: 's1', path: '/share/token', url: 'https://example.org/prefix/share/token' });
      }),
    );
    actions();
    fireEvent.click(screen.getByRole('button', { name: 'Copy response' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Useful answer'));
    const share = screen.getByRole('button', { name: /Share this response/ });
    fireEvent.click(share);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://example.org/prefix/share/token'));
    await waitFor(() => expect(share).toHaveClass('text-state-success'));
    await waitFor(() => expect(share).not.toHaveClass('text-state-success'), { timeout: 3500 });
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
