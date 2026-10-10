import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { afterEach, expect, it, vi } from 'vitest';
import { server } from '@/__tests__/msw-handlers';
import { SteerInputControl } from '../SteerInputControl';
import { routeAgentSignalWith } from '@/lib/api/sse/route-signal';
import { useChatStreamStore } from '@/stores/chat-stream';
import type { ChatInputReceipt } from '@/lib/api/chat-inputs';

const key = '/api/v1/chats/chat/runs/run/input-controls';
afterEach(() => { cleanup(); useChatStreamStore.getState().reset(); });
function setup(options: { supported?: boolean; disabled?: boolean; fail?: boolean } = {}) {
  let receipt: ChatInputReceipt | null = null;
  let posts = 0;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const submitted = vi.fn();
  server.use(
    http.get(`*${key}`, () => HttpResponse.json({ chat_id: 'chat', run_id: 'run',
      supported_controls: options.supported === false ? [] : ['steer'],
      available_controls: options.supported === false || receipt ? [] : ['steer'],
      latest_input: receipt, unavailable_reason: null })),
    http.post('*/api/v1/chats/chat/inputs', async ({ request }) => {
      posts++;
      if (options.fail) return HttpResponse.error();
      const body = await request.json() as { input_id: string; text: string };
      receipt = { ...body, chat_id: 'chat', run_id: 'run', status: 'submitting', error_code: null };
      return HttpResponse.json(receipt, { status: 202 });
    }),
  );
  const element = <QueryClientProvider client={client}><SteerInputControl chatId="chat" runId="run"
    text="Additional instruction" active disabled={!!options.disabled} onSubmitted={submitted} /></QueryClientProvider>;
  return { client, submitted, element, posts: () => posts, receipt: () => receipt };
}

it('submits explicitly once and displays awaiting confirmation rather than accepted', async () => {
  const test = setup(); render(test.element);
  fireEvent.click(await screen.findByRole('button', { name: 'Add to current response' }));
  await waitFor(() => expect(test.submitted).toHaveBeenCalledWith('Additional instruction'));
  expect(await screen.findByText('Submitted, awaiting Agent confirmation')).toBeTruthy();
  expect(test.posts()).toBe(1);
  expect(screen.getByRole('button', { name: 'Add to current response' })).toBeDisabled();
  cleanup(); render(test.element);
  expect(await screen.findByText('Submitted, awaiting Agent confirmation')).toBeTruthy();
  expect(test.posts()).toBe(1);
});

it('keeps the draft and does not retry a failed submission', async () => {
  const test = setup({ fail: true }); render(test.element);
  fireEvent.click(await screen.findByRole('button', { name: 'Add to current response' }));
  expect(await screen.findByText(/Submission is unconfirmed/)).toBeTruthy();
  expect(test.submitted).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Check input status' }));
  await waitFor(() => expect(test.client.isFetching()).toBe(0));
  expect(test.posts()).toBe(1);
});

it('does not offer steering for an old runtime and respects disabled draft inputs', async () => {
  const old = setup({ supported: false }); render(old.element);
  await waitFor(() => expect(old.client.getQueryData(['chat-input-controls', 'chat', 'run', true])).toBeTruthy());
  expect(screen.queryByRole('button', { name: 'Add to current response' })).toBeNull();
  cleanup();
  const disabled = setup({ disabled: true }); render(disabled.element);
  expect(await screen.findByRole('button', { name: 'Add to current response' })).toBeDisabled();
});

it('replays accepted input by stable ID without clearing assistant output or accepting a stale turn', () => {
  const client = new QueryClient();
  const store = useChatStreamStore.getState();
  store.beginTurn('chat', 'run');
  store.applyEvent({ type: 'message_start', message_id: 'assistant', role: 'assistant', content: 'Before' }, 'chat');
  const payload = { input_id: 'input', turn_id: 'run', status: 'accepted', text: 'Add this' };
  const route = (data: unknown) => routeAgentSignalWith(client, 'USER_INPUT_STATUS', data, { wfId: 'scope', chatId: 'chat' });
  route({ ...payload, status: 'unknown' });
  expect(useChatStreamStore.getState().runtimes.chat.messages).toHaveLength(1);
  route(payload); route(payload); route({ ...payload, turn_id: 'old', input_id: 'wrong' });
  store.applyEvent({ type: 'message_delta', message_id: 'assistant', delta: ' after' }, 'chat');
  const runtime = useChatStreamStore.getState().runtimes.chat;
  expect(runtime.messages.map(m => [m.id, m.role, m.content])).toEqual([
    ['assistant', 'assistant', 'Before after'], ['input', 'user', 'Add this'],
  ]);
  expect(runtime.state).toBe('streaming');
});

it('restores an uncertain receipt after reopening a completed chat without an active runtime', async () => {
  server.use(http.get('*/api/v1/chats/chat/input-controls', () => HttpResponse.json({
    chat_id: 'chat', run_id: 'ended', supported_controls: ['steer'], available_controls: [],
    unavailable_reason: 'turn_not_running', latest_input: { input_id: 'pending', chat_id: 'chat', run_id: 'ended',
      text: 'Preserved input', status: 'unknown', error_code: 'turn_ended_before_confirmation' },
  })));
  const client = new QueryClient();
  render(<QueryClientProvider client={client}><SteerInputControl chatId="chat" runId={null}
    text="" active={false} disabled={false} onSubmitted={vi.fn()} /></QueryClientProvider>);
  expect(await screen.findByText(/Receipt is uncertain/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Add to current response' })).toBeNull();
});
