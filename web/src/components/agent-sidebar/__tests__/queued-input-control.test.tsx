import '@/lib/i18n';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { afterEach, expect, it } from 'vitest';
import { server } from '@/__tests__/msw-handlers';
import { queueChatInput, type QueuedChatInput } from '@/lib/api/chat-queue';
import { QueuedInputControl } from '../QueuedInputControl';
afterEach(cleanup);

it.each([false, true])('preserves the queued receipt and cancels once, lost response=%s', async loseResponse => {
  let receipt: QueuedChatInput | null = null;
  let posts = 0;
  let deletes = 0;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  server.use(
    http.get('*/api/v1/chats/chat/queued-input', () => HttpResponse.json({ input: receipt })),
    http.post('*/api/v1/chats/chat/queued-input', async ({ request }) => {
      posts++;
      const body = await request.json() as Pick<QueuedChatInput, 'input_id' | 'after_run_id' | 'message'>;
      receipt = { ...body, chat_id: 'chat', status: 'queued', error_code: null, started_run_id: null,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
      return loseResponse ? HttpResponse.error() : HttpResponse.json(receipt, { status: 201 });
    }),
    http.delete('*/api/v1/chats/chat/queued-input/:id', () => {
      deletes++;
      receipt = { ...receipt!, status: 'cancelled' };
      return HttpResponse.json(receipt);
    }),
  );
  const onQueue = (inputId: string) => queueChatInput('chat', { input_id: inputId, after_run_id: 'run',
    message: { role: 'user', content: 'Next instruction', mode: 'chat', approval_mode: 'always_ask',
      surface: 'main', agent_surface: 'chat', attachments: [] } });
  const element = <QueryClientProvider client={client}><QueuedInputControl chatId="chat" active disabled={false} onQueue={onQueue} /></QueryClientProvider>;
  render(element);
  const button = await screen.findByRole('button', { name: 'Queue next' });
  await waitFor(() => expect(button).toBeEnabled());
  expect(posts).toBe(0);
  fireEvent.click(button);
  expect(await screen.findByText('Queued for next turn')).toBeTruthy();
  expect(button).toBeDisabled();
  cleanup(); render(element);
  const details = document.querySelector('[data-role=queued-input-details]');
  expect(details).not.toHaveAttribute('open');
  fireEvent.click(details!.querySelector('summary')!);
  expect(await screen.findByText('Next instruction')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel queued message' }));
  expect(await screen.findByText('Queue cancelled')).toBeTruthy();
  expect(posts).toBe(1);
  expect(deletes).toBe(1);
});
